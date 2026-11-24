#!/usr/bin/env python3
"""Western WA outage alert starter. Python 3.11+, standard library only.

County-level proxy, not a single-incident detector or an exact Cascade-crest
geofence. See README.md before enabling live delivery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOG = logging.getLogger("outage-bot")
COUNTIES = {
    "53009": "Clallam", "53011": "Clark", "53015": "Cowlitz",
    "53027": "Grays Harbor", "53029": "Island", "53031": "Jefferson",
    "53033": "King", "53035": "Kitsap", "53041": "Lewis", "53045": "Mason",
    "53049": "Pacific", "53053": "Pierce", "53055": "San Juan",
    "53057": "Skagit", "53059": "Skamania", "53061": "Snohomish",
    "53067": "Thurston", "53069": "Wahkiakum", "53073": "Whatcom",
}


def load_env(path: str = ".env") -> None:
    """Read plain KEY=value pairs, without shell evaluation or overriding env."""
    if not Path(path).is_file():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            raise ValueError("Invalid .env line; use plain KEY=value pairs")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).lower()
    if raw not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError(f"{name} must be true or false")
    return raw in {"true", "1", "yes"}


def parse_time(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError("An ISO-8601 timestamp with a timezone is required")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return dt.timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def local_time(ts: float) -> str:
    try:
        tz = ZoneInfo("America/Los_Angeles")
    except ZoneInfoNotFoundError:
        tz = timezone.utc  # On Windows, install tzdata to get Pacific time.
    return datetime.fromtimestamp(ts, tz).strftime("%b %d %I:%M %p %Z")


def nonnegative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Customer count must be a nonnegative integer")
    return value


def safe_url(value: str) -> str:
    if not isinstance(value, str) or any(c in value for c in "<>|\r\n"):
        raise ValueError("Invalid source URL")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise ValueError("Source URLs must be HTTPS without embedded credentials")
    return value


@dataclass(frozen=True)
class Config:
    threshold: int = 10_000
    hold_seconds: int = 15 * 60
    reset_below: int = 5_000
    reset_seconds: int = 30 * 60
    max_age: int = 20 * 60
    max_gap: int = 15 * 60
    poll_seconds: int = 5 * 60
    scope: str = "regional"
    mode: str = "balanced"
    max_posts_hour: int = 2
    escalation_seconds: int = 30 * 60
    escalation_absolute: int = 10_000
    recovery_posts: bool = False
    digest_seconds: int = 60 * 60
    regional_max_skew: int = 5 * 60
    counties: dict[str, str] = field(default_factory=lambda: dict(COUNTIES))
    source_kind: str = "utility"
    utility_sources: tuple[str, ...] = ("pse",)
    optional_utility_sources: tuple[str, ...] = ()


    def validate(self) -> None:
        if self.scope not in {"county", "regional"}:
            raise ValueError("ALERT_SCOPE must be county or regional")
        if self.source_kind not in {"massoutage", "json", "utility"}:
            raise ValueError("OUTAGE_SOURCE must be massoutage, json, or utility")
        if not self.mode in {"quiet", "balanced", "digest"}:
            raise ValueError("ALERT_MODE must be quiet, balanced, or digest")
        if not (0 < self.reset_below < self.threshold):
            raise ValueError("Require 0 < RESET_BELOW < THRESHOLD")
        for value in (self.hold_seconds, self.reset_seconds, self.max_age,
                      self.max_gap, self.poll_seconds, self.max_posts_hour,
                      self.escalation_seconds, self.escalation_absolute,
                      self.digest_seconds, self.regional_max_skew):
            if value <= 0:
                raise ValueError("Durations, limits, and thresholds must be positive")
        if self.poll_seconds < 60:
            raise ValueError("POLL_SECONDS must be at least 60")
        if not self.counties or any(k not in COUNTIES for k in self.counties):
            raise ValueError("COUNTY_FIPS must be a nonempty subset of the included counties")
        if self.source_kind == "utility" and not self.utility_sources:
            raise ValueError("UTILITY_SOURCES must name at least one required utility")
        if self.source_kind == "utility" and set(self.utility_sources) & set(self.optional_utility_sources):
            raise ValueError("UTILITY_SOURCES and OPTIONAL_UTILITY_SOURCES must not overlap")

    @classmethod
    def from_env(cls) -> Config:
        if "ESCALATION_FRACTION" in os.environ:
            LOG.warning("ESCALATION_FRACTION is no longer used; growth alerts use "
                        "ESCALATION_ABSOLUTE only")
        fields = {
            "threshold": ("THRESHOLD", int), "hold_seconds": ("HOLD_SECONDS", int),
            "reset_below": ("RESET_BELOW", int), "reset_seconds": ("RESET_SECONDS", int),
            "max_age": ("MAX_DATA_AGE_SECONDS", int), "max_gap": ("MAX_SAMPLE_GAP_SECONDS", int),
            "poll_seconds": ("POLL_SECONDS", int), "scope": ("ALERT_SCOPE", str),
            "mode": ("ALERT_MODE", str), "max_posts_hour": ("MAX_POSTS_PER_HOUR", int),
            "source_kind": ("OUTAGE_SOURCE", str),
            "escalation_seconds": ("ESCALATION_COOLDOWN_SECONDS", int),
            "escalation_absolute": ("ESCALATION_ABSOLUTE", int),
            "digest_seconds": ("DIGEST_SECONDS", int),
            "regional_max_skew": ("REGIONAL_MAX_SKEW_SECONDS", int),
        }
        args = {key: conv(os.environ[env]) for key, (env, conv) in fields.items() if env in os.environ}
        args["recovery_posts"] = env_bool("SEND_RECOVERY", False)
        utilities = tuple(x.strip().lower() for x in os.getenv("UTILITY_SOURCES", "pse").split(",") if x.strip())
        optional_utilities = tuple(x.strip().lower() for x in os.getenv("OPTIONAL_UTILITY_SOURCES", "").split(",") if x.strip())
        args["utility_sources"] = utilities
        args["optional_utility_sources"] = optional_utilities
        if os.getenv("COUNTY_FIPS"):
            selected = os.environ["COUNTY_FIPS"].split(",")
            args["counties"] = {key.strip(): COUNTIES.get(key.strip(), "UNKNOWN") for key in selected}
        result = cls(**args)
        result.validate()
        return result

    def fingerprint(self) -> str:
        # Changing semantics must not accidentally reuse incompatible timers.
        values = (self.scope, self.threshold, self.hold_seconds, self.reset_below,
                  self.reset_seconds, self.max_gap, self.source_kind, self.utility_sources, self.optional_utility_sources, sorted(self.counties))
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    key: str
    name: str
    count: int
    observed_at: float
    source: str
    url: str
    recovery_allowed: bool = True
    # The time used to validate the age of the underlying source data can be
    # older than the rollup poll time. Keep it separate from observed_at,
    # which is the sample time used by the duration engine.
    freshness_at: float | None = None

    def valid(self, now: float, cfg: Config) -> bool:
        freshness = self.observed_at if self.freshness_at is None else self.freshness_at
        return (isinstance(self.count, int) and not isinstance(self.count, bool)
                and self.count >= 0 and math.isfinite(self.observed_at)
                and math.isfinite(freshness)
                and -60 <= now - freshness <= cfg.max_age
                and self.observed_at - now <= 60)


class RemoteError(RuntimeError):
    def __init__(self, message: str, retry_after: int = 300):
        super().__init__(message)
        self.retry_after = max(1, retry_after)


def http_request(url: str, *, payload: dict | None = None,
                 headers: dict | None = None, raw: bool = False) -> Any:
    """One bounded HTTP request; no secrets are included in errors or logs."""
    safe_url(url)
    hdr = {"User-Agent": "WesternWAOutageBot/0.1", "Accept": "application/json"}
    hdr.update(headers or {})
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    if data is not None:
        hdr["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            body = response.read(5_000_001)
            if len(body) > 5_000_000:
                raise RemoteError("Response exceeded the size limit")
            return body.decode("utf-8") if raw else json.loads(body)
    except urllib.error.HTTPError as exc:
        try:
            delay = int(exc.headers.get("Retry-After", "300"))
        except ValueError:
            delay = 300
        raise RemoteError(f"Remote HTTP {exc.code}", delay) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RemoteError(f"Remote request failed ({type(exc).__name__})") from None


class MassOutageProvider:
    """Adapter to MassOutage's documented v1 live county API.

    Uses one feed-health request plus one live county request per configured
    county. The free API key is sent only as an Authorization header and is
    never written to logs or alert messages.
    """
    base = "https://massoutage.com/api/v1"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        key = os.getenv("MASSOUTAGE_API_KEY", "")
        if cfg.poll_seconds < 300:
            raise ValueError("MassOutage requests must be at least five minutes apart")
        if not key and len(cfg.counties) > 4:
            raise ValueError("A free MASSOUTAGE_API_KEY is required to monitor this many counties")
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.retry_after = 0

    @staticmethod
    def decode(fips: str, payload: dict, *, feed_degraded: bool = False) -> Snapshot:
        data = payload["data"]
        if data["fips"] != fips:
            raise ValueError("Wrong county returned")
        # The documented /live/counties/{fips} schema exposes customers_out
        # at data.customers_out. Accept data.live.customers_out as a defensive
        # compatibility path for profile-shaped responses, but do not guess.
        live_data = data.get("live") if isinstance(data.get("live"), dict) else {}
        raw_count = data.get("customers_out", live_data.get("customers_out"))
        count = nonnegative_int(raw_count)
        live = payload.get("meta", {}).get("live", {})
        if live.get("status") == "delayed":
            raise ValueError("Provider marked the data delayed")
        if count:
            # MassOutage distinguishes two timestamps:
            #   updated_at = time this county rollup was polled/generated
            #   as_of     = oldest underlying utility-feed fetch contributing
            # Use updated_at as the sample time for duration tracking. Validate
            # freshness against the older timestamp so carried-forward source
            # data is still rejected when it exceeds the configured age.
            updated = parse_time(data["updated_at"])
            as_of = parse_time(data["as_of"])
            stamp = updated
            freshness_stamp = min(updated, as_of)
        else:
            # Null timestamps are common at zero; require explicit poll health.
            if live.get("status") != "ok":
                raise ValueError("Zero count without explicit fresh-poll evidence")
            stamp = parse_time(live["last_poll_ts"])
            freshness_stamp = stamp
        return Snapshot(fips, COUNTIES[fips] + " County", count, stamp,
                        "MassOutage", "https://massoutage.com/washington/" +
                        COUNTIES[fips].lower().replace(" ", "-"),
                        recovery_allowed=not feed_degraded, freshness_at=freshness_stamp)

    def fetch(self) -> tuple[dict[str, Snapshot], list[str]]:
        result: dict[str, Snapshot] = {}
        issues: list[str] = []
        self.retry_after = 0
        try:
            health = http_request(self.base + "/live/sources", headers=self.headers)["data"]
            coverage = health.get("coverage", {})
            feeds = health.get("feeds", {})
            sources_ok = coverage.get("sources_ok")
            if sources_ok is None:
                raise ValueError("Provider health response omitted coverage.sources_ok")
            if int(sources_ok) <= 0:
                raise ValueError("No successful upstream feeds")
            degraded = (int(feeds.get("down", 0) or 0) > 0 or
                        int(feeds.get("stale", 0) or 0) > 0)
            if degraded:
                issues.append("Provider reports degraded feeds; rearming is disabled this poll")
        except (RemoteError, KeyError, TypeError, ValueError) as exc:
            self.retry_after = getattr(exc, "retry_after", 0)
            return {}, [f"Provider health unavailable ({type(exc).__name__})"]
        for fips in self.cfg.counties:
            try:
                raw = http_request(self.base + "/live/counties/" + fips, headers=self.headers)
                result[fips] = self.decode(fips, raw, feed_degraded=degraded)
            except RemoteError as exc:
                issues.append(f"{fips}: {exc}")
                self.retry_after = max(self.retry_after, exc.retry_after)
                # Stop on 429 rather than continuing to hit the provider.
                if "429" in str(exc):
                    break
            except (KeyError, TypeError, ValueError) as exc:
                issues.append(f"{fips}: invalid or uncertain data ({type(exc).__name__})")
            time.sleep(0.25)
        return result, issues



class PSEProvider:
    """Adapter for PSE's public outage-map JSON feed.

    The outage-map page is a client-side application. PSE currently exposes
    the underlying JSON response at the endpoint used by its map. This is not
    a documented public API, so the adapter is deliberately conservative and
    fails closed on schema changes.
    """
    name = "PSE"
    default_url = "https://www.pse.com/api/sitecore/OutageMap/AnonymoussMapListView"
    retry_after = 0

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.url = os.getenv("PSE_OUTAGE_URL", self.default_url)

    @staticmethod
    def _attr_map(item: dict) -> dict[str, str]:
        attrs = item.get("DataProvider", {}).get("Attributes", [])
        out: dict[str, str] = {}
        if isinstance(attrs, list):
            for attr in attrs:
                if not isinstance(attr, dict):
                    continue
                name = attr.get("RefName") or attr.get("Name")
                value = attr.get("Value")
                if isinstance(name, str) and isinstance(value, str):
                    out[name.strip().lower()] = value.strip()
        return out

    @staticmethod
    def _pacific_tz_for_date(dt: datetime):
        # Prefer the IANA zone when available. Windows installations often lack
        # the tzdata package, so fall back to the US Pacific DST rules.
        # Normalize aware inputs to a naive local-wall-clock value before
        # comparing against naive DST transition boundaries.
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        try:
            return ZoneInfo("America/Los_Angeles")
        except ZoneInfoNotFoundError:
            # US DST: from the second Sunday in March through the first Sunday
            # in November. PSE timestamps are local wall-clock times, so this
            # is sufficient for the feed's minute-level freshness checks.
            def nth_weekday(year: int, month: int, weekday: int, n: int) -> int:
                first = datetime(year, month, 1)
                return 1 + ((weekday - first.weekday()) % 7) + 7 * (n - 1)
            dst_start = datetime(dt.year, 3, nth_weekday(dt.year, 3, 6, 2), 2)
            dst_end = datetime(dt.year, 11, nth_weekday(dt.year, 11, 6, 1), 2)
            if dst_start <= dt < dst_end:
                return timezone(timedelta(hours=-7), name="PDT")
            return timezone(timedelta(hours=-8), name="PST")

    @staticmethod
    def _parse_local_stamp(value: str, now: float) -> float:
        # PSE's map feed reports timestamps in Pacific local time and omits the
        # timezone/year. Interpret them as Pacific local time, then convert to
        # UTC for freshness calculations.
        try:
            local_now = datetime.fromtimestamp(now, timezone.utc).astimezone(
                PSEProvider._pacific_tz_for_date(datetime.fromtimestamp(now, timezone.utc))
            )
            if re.fullmatch(r"\d{1,2}/\d{1,2} \d{1,2}:\d{2} [AP]M", value):
                year = local_now.year
                parts = datetime.strptime(f"{year} {value}", "%Y %m/%d %I:%M %p")
                tz = PSEProvider._pacific_tz_for_date(parts)
                dt = parts.replace(tzinfo=tz)
                if dt.timestamp() - now > 183 * 86400:
                    parts = parts.replace(year=year - 1)
                    dt = parts.replace(tzinfo=PSEProvider._pacific_tz_for_date(parts))
                return dt.timestamp()
            parts = datetime.strptime(value, "%m/%d/%Y %I:%M %p")
            return parts.replace(tzinfo=PSEProvider._pacific_tz_for_date(parts)).timestamp()
        except ValueError:
            pass
        raise ValueError("Unparseable PSE timestamp")

    @classmethod
    def parse_body(cls, payload: Any, now: float) -> list[dict]:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return []
        if not isinstance(payload, dict):
            raise ValueError("PSE payload is not an object")
        items = payload.get("PseMap")
        if not isinstance(items, list):
            return []
        rows: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            poi = item.get("DataProvider", {}).get("PointOfInterest", {})
            if not isinstance(poi, dict):
                continue
            # Planned/scheduled outages are excluded from the emergency alert total.
            if poi.get("PlannedOutage") is True or str(poi.get("FilterType", "")).lower() == "planned":
                continue
            attrs = cls._attr_map(item)
            oid = poi.get("Id") or attrs.get("outageeventid")
            count_raw = attrs.get("customers impacted")
            start_raw = attrs.get("startdate")
            update_raw = attrs.get("lastupdated")
            if not oid or count_raw is None or start_raw is None:
                continue
            try:
                count = int(str(count_raw).replace(",", ""))
                if count < 0:
                    continue
                start = cls._parse_local_stamp(start_raw, now)
                update = cls._parse_local_stamp(update_raw, now) if update_raw else now
            except (TypeError, ValueError):
                continue
            rows.append({
                "id": str(oid),
                "count": count,
                "start": start,
                "observed": min(update, now),
                "status": attrs.get("status", "active"),
                "cause": attrs.get("cause", ""),
            })
        if not rows and items:
            raise ValueError("PSE payload contained outage records but none were parseable")
        return rows

    def fetch(self) -> tuple[dict[str, Snapshot], list[str]]:
        try:
            payload = http_request(self.url)
            now = time.time()
            # PSE publishes an authoritative unplanned-outage customer total in
            # the summary. Incident rows are useful for diagnostics, but they
            # must not be a prerequisite for the regional total: the map can
            # omit, cluster, or change individual records while the summary
            # remains valid. Parse rows opportunistically only.
            try:
                rows = self.parse_body(payload, now)
            except ValueError:
                rows = []
            if not isinstance(payload, dict):
                raise TypeError("PSE response JSON was not an object")
            summary = payload.get("UnplannedOutageSummary") or {}
            if not isinstance(summary, dict):
                raise TypeError("PSE UnplannedOutageSummary was not an object")
            summary_count = summary.get("CustomerAfftectedCount")
            if summary_count is not None:
                # Prefer the provider's explicit unplanned total. It avoids any
                # accidental omission if a record is represented by a map cluster.
                count = int(str(summary_count).replace(",", "").strip())
            else:
                count = sum(r["count"] for r in rows)
            if count < 0:
                raise ValueError("PSE returned a negative customer count")
            updated_raw = summary.get("LastUpdatedDatetime")
            # The endpoint can remain reachable while its embedded timestamp is old.
            # Preserve the source value, but only a fresh timestamp should qualify for alert timing.
            if updated_raw:
                observed = self._parse_local_stamp(updated_raw, now)
            elif rows:
                observed = min(r["observed"] for r in rows)
            else:
                raise ValueError("PSE response had no freshness timestamp")
            age = max(0.0, now - observed)
            LOG.info("PSE feed responded: %d customers, %d incidents, data age=%ds", count, len(rows), int(age))
            if age > self.cfg.max_age:
                return {"pse": Snapshot("pse", "Puget Sound Energy", count, observed, "PSE", self.url, recovery_allowed=False)}, [f"PSE data stale ({int(age)}s old)"]
            return {"pse": Snapshot("pse", "Puget Sound Energy", count, observed, "PSE", self.url, recovery_allowed=True)}, []
        except (RemoteError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.retry_after = getattr(exc, "retry_after", self.cfg.poll_seconds)
            LOG.warning("PSE source error: %s: %s", type(exc).__name__, exc)
            return {}, [f"PSE source unavailable ({type(exc).__name__}): {exc}"]



class PowerOutageTrackerUtilityProvider:
    """PowerOutageTracker documented JSON state feed for ODIN-participating utilities."""
    specs = {
        "scl": ("Seattle City Light", ("seattle city light",)),
        "snopud": ("Snohomish County PUD", ("snohomish county pud", "pud no 1 of snohomish county")),
    }
    url = "https://poweroutagetracker.com/api/v1/states/washington"

    def __init__(self, cfg: Config, key: str):
        if key not in self.specs:
            raise ValueError(f"Unsupported PowerOutageTracker utility: {key}")
        self.cfg = cfg
        self.key = key
        self.name, self.match_terms = self.specs[key]
        self.retry_after = 0

    @staticmethod
    def _norm_text(value: Any) -> str:
        return re.sub(r"\\s+", " ", str(value).strip().lower())

    @classmethod
    def _matches_utility(cls, d: dict[str, Any], needles: tuple[str, ...]) -> bool:
        for key in ("utility", "utility_name", "utilityName", "provider", "provider_name", "company", "company_name", "name"):
            value = d.get(key)
            if isinstance(value, str):
                text = cls._norm_text(value)
                if any(needle in text for needle in needles):
                    return True
        return False

    @staticmethod
    def _parse_timestamp(value: Any) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            x = float(value)
            if x > 10_000_000_000:
                x /= 1000.0
            if 0 < x < 4_000_000_000:
                return x
            return None
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        try:
            return parse_time(text)
        except (ValueError, TypeError):
            pass
        # Common API format without explicit zone: assume UTC for feed timestamps.
        for fmt in ("%b %d, %Y %I:%M %p %Z", "%b %d, %I:%M %p %Z"):
            try:
                dt = datetime.strptime(text, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue
        return None

    @classmethod
    def _extract_out_count(cls, d: dict[str, Any]) -> int | None:
        preferred = (
            "customers_out", "customersOut", "customers_without_power", "customersWithoutPower",
            "customers_affected", "customersAffected", "affected_customers", "affectedCustomers",
            "outage_customers", "outageCustomers",
        )
        for key in preferred:
            value = d.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
            if isinstance(value, str) and value.replace(',', '').isdigit():
                return int(value.replace(',', ''))
        return None

    @classmethod
    def _walk(cls, obj: Any):
        if isinstance(obj, dict):
            yield obj
            for value in obj.values():
                yield from cls._walk(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from cls._walk(value)

    @classmethod
    def _extract_updated_times(cls, d: dict[str, Any]) -> list[float]:
        out = []
        for key, value in d.items():
            k = re.sub(r"[^a-z]", "", str(key).lower())
            if any(token in k for token in ("lastupdate", "lastupdated", "updatedat", "updated", "asof", "timestamp", "lastseen")):
                ts = cls._parse_timestamp(value)
                if ts is not None:
                    out.append(ts)
        return out

    @classmethod
    def parse_payload(cls, payload: Any, key: str, name: str, match_terms: tuple[str, ...], now: float) -> Snapshot:
        if not isinstance(payload, (dict, list)):
            raise TypeError("PowerOutageTracker API response was not JSON object/list")
        candidates: list[tuple[int, float, dict[str, Any]]] = []
        all_times: list[float] = []
        for d in cls._walk(payload):
            all_times.extend(cls._extract_updated_times(d))
            if cls._matches_utility(d, match_terms):
                count = cls._extract_out_count(d)
                if count is not None:
                    times = cls._extract_updated_times(d)
                    stamp = max(times) if times else 0.0
                    candidates.append((count, stamp, d))
        if not candidates:
            raise ValueError(f"PowerOutageTracker API contained no current {name} outage total")
        # Prefer the most specific utility record; if multiple records exist, sum only
        # distinct utility-level records. Utility records with a timestamp are preferred.
        best_stamp = max((stamp for _, stamp, _ in candidates), default=0.0)
        matched = [c for c in candidates if c[1] == best_stamp] if best_stamp else candidates
        count = sum(c[0] for c in matched)
        observed = best_stamp or (max(all_times) if all_times else 0.0)
        if observed <= 0:
            raise ValueError("PowerOutageTracker API contained no freshness timestamp")
        age = max(0.0, now - observed)
        if age > 20 * 60:
            raise ValueError(f"{name} data stale ({int(age)}s old)")
        return Snapshot(key, name, count, observed, "PowerOutageTracker", cls.url, True)

    def fetch(self) -> tuple[dict[str, Snapshot], list[str]]:
        try:
            payload = http_request(self.url)
            snap = self.parse_payload(payload, self.key, self.name, self.match_terms, time.time())
            LOG.info("%s API responded: %d customers, data age=%ds", self.name, snap.count, int(max(0, time.time() - snap.observed_at)))
            return {self.key: snap}, []
        except RemoteError as exc:
            self.retry_after = exc.retry_after
            return {}, [f"{self.name} source unavailable (RemoteError): {exc}"]
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return {}, [f"{self.name} source unavailable ({type(exc).__name__}): {exc}"]

class ArcGISProvider:
    """Generic ArcGIS FeatureServer outage adapter.

    The utility must expose a public FeatureServer endpoint. The adapter is
    deliberately configurable because utilities can change service URLs.
    """
    def __init__(self, cfg: Config, key: str, name: str, env_name: str):
        self.cfg, self.key, self.name, self.env_name = cfg, key, name, env_name
        defaults = {"TACOMA_OUTAGE_URL": "https://services.arcgis.com/BLN4oKB0N1YSgvY8/ArcGIS/rest/services/Power_Outages_%28View%29/FeatureServer/0"}
        self.url = os.getenv(env_name, defaults.get(env_name, "")).strip()
        self.retry_after = 0

    def fetch(self) -> tuple[dict[str, Snapshot], list[str]]:
        if not self.url:
            return {}, [f"{self.name} disabled: set {self.env_name} to its public FeatureServer URL"]
        try:
            base = self.url.rstrip("/")
            where = "1=1"
            if self.key == "tacoma":
                where = "UtilityCompany = 'Tacoma Power' AND OutageStatus = 'Active' AND OutageType = 'Not Planned'"
            query = base + ("&" if "?" in base else "?") + urllib.parse.urlencode({
                "where": where, "outFields": "*", "returnGeometry": "false", "f": "json"
            })
            payload = http_request(query)
            metadata = http_request(base)
            rows, issues = self.parse_payload(payload, allow_missing_timestamp=(self.key == "tacoma"))
            # The FeatureServer layer metadata exposes the last data edit time; use
            # it as the source freshness timestamp rather than the oldest incident
            # start time, which can legitimately be days old.
            edit_ms = None
            if isinstance(metadata, dict):
                edit = metadata.get("editingInfo")
                if isinstance(edit, dict):
                    edit_ms = edit.get("lastEditDate")
                if edit_ms is None:
                    edit_ms = metadata.get("lastEditDate")
            if edit_ms is None:
                return {}, issues + [f"{self.name} response has no layer freshness timestamp"]
            stamp = float(edit_ms) / 1000.0 if float(edit_ms) > 10_000_000_000 else float(edit_ms)
            snap = rows.get(self.key)
            if snap is None:
                now = time.time()
                snap = Snapshot(self.key, self.name, 0, stamp, self.name, self.url, True)
            else:
                snap = Snapshot(self.key, self.name, snap.count, stamp, self.name, self.url, True)
            age = max(0.0, time.time() - stamp)
            if age > self.cfg.max_age:
                return {self.key: snap}, issues + [f"{self.name} data stale ({int(age)}s old)"]
            return {self.key: snap}, issues
        except (RemoteError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.retry_after = getattr(exc, "retry_after", self.cfg.poll_seconds)
            return {}, [f"{self.name} source unavailable ({type(exc).__name__}): {exc}"]

    def parse_payload(self, payload: Any, *, allow_missing_timestamp: bool = False) -> tuple[dict[str, Snapshot], list[str]]:
        if not isinstance(payload, dict):
            raise TypeError(f"{self.name} response is not JSON object")
        features = payload.get("features")
        if not isinstance(features, list):
            raise ValueError(f"{self.name} response has no features list")
        if not features:
            # Empty incident set is a valid fresh zero only if the service provides
            # an explicit update timestamp; otherwise we cannot safely infer freshness.
            now = time.time()
            return {self.key: Snapshot(self.key, self.name, 0, now, self.name, self.url, True)}, []
        total = 0
        observed = None
        for feature in features:
            attrs = feature.get("attributes") if isinstance(feature, dict) else None
            if not isinstance(attrs, dict):
                continue
            total += self._count(attrs)
            ts = self._timestamp(attrs)
            if ts is not None:
                observed = ts if observed is None else min(observed, ts)
        if observed is None and not allow_missing_timestamp:
            raise ValueError(f"{self.name} features have no usable freshness timestamp")
        stamp = observed if observed is not None else time.time()
        return {self.key: Snapshot(self.key, self.name, total, stamp, self.name, self.url, True)}, []

    @staticmethod
    def _first(attrs: dict, names: tuple[str, ...]):
        lowered = {str(k).lower(): v for k, v in attrs.items()}
        for name in names:
            if name.lower() in lowered:
                return lowered[name.lower()]
        return None

    @classmethod
    def _count(cls, attrs: dict) -> int:
        raw = cls._first(attrs, (
            "customersaffected", "customers_affected", "customersout", "customers_out",
            "customerimpacted", "customersimpacted", "custout", "affectedcustomers",
            "customerswithoutpower", "customerswithoutpwr", "cust_without_power"
        ))
        if raw is None:
            raise ValueError("FeatureServer outage record has no recognizable customer-count field")
        try:
            value = int(float(str(raw).replace(",", "").strip()))
        except ValueError as exc:
            raise ValueError("FeatureServer outage customer count is not numeric") from exc
        if value < 0:
            raise ValueError("FeatureServer outage customer count is negative")
        return value

    @classmethod
    def _timestamp(cls, attrs: dict) -> float | None:
        raw = cls._first(attrs, (
            "updated", "updatedat", "lastupdated", "last_updated", "last_update", "lastupdateddate",
            "modified", "editdate", "outage_start", "starttime", "start_time",
            "outagetime", "reported", "reportedat"
        ))
        if raw in (None, ""):
            return None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            # ArcGIS commonly returns epoch milliseconds.
            value = float(raw)
            if value > 10_000_000_000:
                value /= 1000.0
            return value
        if isinstance(raw, str):
            text = raw.strip()
            try:
                return parse_time(text)
            except ValueError:
                pass
            for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %I:%M:%S %p"):
                try:
                    dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
                    return dt.timestamp()
                except ValueError:
                    continue
        return None


class JSONUtilityProvider:
    """Generic utility JSON adapter for a documented or explicitly configured feed."""
    def __init__(self, cfg: Config, key: str, name: str, env_name: str):
        self.cfg, self.key, self.name, self.env_name = cfg, key, name, env_name
        self.url = os.getenv(env_name, "").strip()
        self.retry_after = 0

    def fetch(self) -> tuple[dict[str, Snapshot], list[str]]:
        if not self.url:
            return {}, [f"{self.name} disabled: set {self.env_name} to its official JSON endpoint"]
        try:
            payload = http_request(self.url)
            return self.parse_payload(payload)
        except (RemoteError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self.retry_after = getattr(exc, "retry_after", self.cfg.poll_seconds)
            return {}, [f"{self.name} source unavailable ({type(exc).__name__}): {exc}"]

    def parse_payload(self, payload: Any, *, allow_missing_timestamp: bool = False) -> tuple[dict[str, Snapshot], list[str]]:
        if not isinstance(payload, dict):
            raise TypeError(f"{self.name} response is not JSON object")
        rows = payload.get("outages") or payload.get("features") or payload.get("incidents")
        if isinstance(rows, list):
            total = 0
            observed = None
            for row in rows:
                attrs = row.get("attributes", row) if isinstance(row, dict) else {}
                if not isinstance(attrs, dict):
                    continue
                total += ArcGISProvider._count(attrs)
                ts = ArcGISProvider._timestamp(attrs)
                if ts is not None:
                    observed = ts if observed is None else min(observed, ts)
            if observed is None:
                raise ValueError(f"{self.name} records have no usable freshness timestamp")
        else:
            # Some utility feeds expose a single summary object.
            attrs = payload.get("summary") or payload.get("data") or payload
            if not isinstance(attrs, dict):
                raise ValueError(f"{self.name} response has no recognizable outage payload")
            total = ArcGISProvider._count(attrs)
            observed = ArcGISProvider._timestamp(attrs)
            if observed is None:
                raise ValueError(f"{self.name} response has no usable freshness timestamp")
        return {self.key: Snapshot(self.key, self.name, total, observed, self.name, self.url, True)}, []



class MassOutageUtilityProvider:
    """Utility-specific totals from MassOutage's free keyless live county API.

    This provider uses one county request per configured county and filters the
    per-utility breakdown locally. It is intended for utilities whose own public
    map does not yet expose a stable documented machine-readable endpoint.
    """
    base = "https://massoutage.com/api/v1/live/counties"
    cache_ttl_seconds = 300

    def __init__(self, cfg: Config, key: str, name: str, county_fips: tuple[str, ...], utility_match: tuple[str, ...]):
        self.cfg = cfg
        self.key = key
        self.name = name
        self.county_fips = county_fips
        self.utility_match = tuple(x.lower() for x in utility_match)
        self.retry_after = 0
        base_cache = Path(os.getenv("MASSOUTAGE_CACHE_DIR", "data/massoutage-cache"))
        self.cache_file = base_cache / f"{key}.json"
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache = self._load_cache()

    def _load_cache(self) -> dict:
        try:
            if self.cache_file.exists():
                payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
                return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            LOG.warning("Ignoring unreadable MassOutage cache file")
        return {}

    def _save_cache(self) -> None:
        tmp = self.cache_file.with_suffix(self.cache_file.suffix + ".tmp")
        tmp.write_text(json.dumps(self.cache, allow_nan=False), encoding="utf-8")
        tmp.replace(self.cache_file)

    def _cache_key(self, fips: str) -> str:
        return f"{self.key}:{fips}"

    def _cached_county(self, fips: str) -> tuple[int | None, float | None, str | None] | None:
        item = self.cache.get(self._cache_key(fips))
        if not isinstance(item, dict):
            return None
        cached_at = item.get("cached_at")
        if not isinstance(cached_at, (int, float)) or time.time() - cached_at >= self.cache_ttl_seconds:
            return None
        count = item.get("count")
        stamp = item.get("stamp")
        if not isinstance(count, int) or not isinstance(stamp, (int, float)):
            return None
        return count, float(stamp), None

    def _matches(self, utility: dict) -> bool:
        name = str(utility.get("name", "")).lower()
        return any(token in name for token in self.utility_match)

    def _fetch_county(self, fips: str) -> tuple[int | None, float | None, str | None]:
        cached = self._cached_county(fips)
        if cached is not None:
            return cached
        payload = http_request(f"{self.base}/{fips}")
        if not isinstance(payload, dict):
            raise TypeError(f"{self.name} county response is not JSON object")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"{self.name} county response has no data object")
        utilities = data.get("utilities")
        if not isinstance(utilities, list):
            raise ValueError(f"{self.name} county response has no utilities list")
        matches = [u for u in utilities if isinstance(u, dict) and self._matches(u)]
        if not matches:
            return None, None, None
        total = 0
        for utility in matches:
            raw = utility.get("customers_out")
            if raw is None:
                return None, None, "customer count unavailable"
            value = int(raw)
            if value < 0:
                raise ValueError("negative customer count")
            total += value
        stamp = data.get("as_of") or data.get("updated_at")
        if not stamp:
            return None, None, "missing freshness timestamp"
        parsed_stamp = parse_time(str(stamp))
        self.cache[self._cache_key(fips)] = {
            "cached_at": time.time(),
            "count": total,
            "stamp": parsed_stamp,
        }
        self._save_cache()
        return total, parsed_stamp, None

    def fetch(self) -> tuple[dict[str, Snapshot], list[str]]:
        totals = 0
        stamps: list[float] = []
        matched_counties = 0
        issues: list[str] = []
        self.retry_after = 0
        for fips in self.county_fips:
            try:
                count, stamp, note = self._fetch_county(fips)
                if note:
                    issues.append(f"{self.name} {fips}: {note}")
                    continue
                if count is None or stamp is None:
                    # An outage-only connector may omit this utility when it is clear.
                    # Do not turn absence into zero.
                    continue
                totals += count
                stamps.append(stamp)
                matched_counties += 1
            except (RemoteError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                retry_after = getattr(exc, "retry_after", self.cfg.poll_seconds)
                self.retry_after = max(self.retry_after, retry_after)
                issues.append(f"{self.name} {fips} unavailable ({type(exc).__name__}): {exc}")
                if isinstance(exc, RemoteError) and "429" in str(exc):
                    issues.append(f"{self.name}: stopping further keyless API calls after HTTP 429; Retry-After={retry_after}s")
                    break
        if matched_counties == 0:
            return {}, issues or [f"{self.name} returned no visible utility rows"]
        observed = min(stamps)
        age = max(0.0, time.time() - observed)
        if age > self.cfg.max_age:
            return {}, issues + [f"{self.name} data stale ({int(age)}s old)"]
        return {self.key: Snapshot(self.key, self.name, totals, observed, "MassOutage", "https://massoutage.com/", True)}, issues


class UtilityAggregateProvider:
    """Aggregate independent utility totals; any missing enabled source makes the region UNKNOWN."""
    PROVIDERS = {
        "pse": PSEProvider,
        "scl": lambda cfg: PowerOutageTrackerUtilityProvider(cfg, "scl"),
        "snopud": lambda cfg: PowerOutageTrackerUtilityProvider(cfg, "snopud"),
        "tacoma": lambda cfg: ArcGISProvider(cfg, "tacoma", "Tacoma Power", "TACOMA_OUTAGE_URL"),
    }

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.providers = []
        for name in cfg.utility_sources + cfg.optional_utility_sources:
            cls = self.PROVIDERS.get(name)
            if cls is None:
                raise ValueError(f"Unsupported utility source: {name}")
            self.providers.append(cls(cfg))
        self.retry_after = 0

    def fetch(self) -> tuple[dict[str, Snapshot], list[str]]:
        result, issues = {}, []
        self.retry_after = 0
        for provider in self.providers:
            rows, errs = provider.fetch()
            issues.extend(errs)
            self.retry_after = max(self.retry_after, getattr(provider, "retry_after", 0))
            result.update(rows)
        return result, issues

class JsonProvider:
    """Normalized file/HTTPS input; vendor adapters must supply this contract."""
    retry_after = 0

    def __init__(self, cfg: Config, location: str):
        self.cfg, self.location = cfg, location

    def fetch(self) -> tuple[dict[str, Snapshot], list[str]]:
        if self.location.startswith("https://"):
            payload = http_request(self.location)
        else:
            payload = json.loads(Path(self.location).read_text(encoding="utf-8"))
        result: dict[str, Snapshot] = {}
        for row in payload["counties"]:
            key = str(row["fips"])
            if key not in self.cfg.counties:
                continue
            if key in result:
                raise ValueError("Duplicate county records; do not sum overlapping sources")
            recovery_allowed = row.get("recovery_allowed", False)
            if not isinstance(recovery_allowed, bool):
                raise ValueError("recovery_allowed must be boolean")
            result[key] = Snapshot(key, self.cfg.counties[key] + " County",
                                   nonnegative_int(row["customers_out"]),
                                   parse_time(row["observed_at"]), str(row["source"]),
                                   safe_url(row["source_url"]), recovery_allowed)
        return result, []


class StateStore:
    """Durable checkpoints. Run one worker per database, not multiple replicas."""
    def __init__(self, filename: str):
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(filename, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()

    def load(self) -> dict:
        row = self.db.execute("SELECT value FROM state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, value: dict) -> None:
        with self.db:
            self.db.execute("INSERT INTO state VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                            (json.dumps(value, allow_nan=False),))

    def close(self) -> None:
        self.db.close()


class Sender(Protocol):
    def send(self, text: str, thread_ts: str | None = None) -> str | None: ...


class ConsoleSender:
    def send(self, text: str, thread_ts: str | None = None) -> str:
        print("\n--- DRY RUN" + (" THREAD" if thread_ts else " POST") + " ---\n" + text, flush=True)
        return "dry-run-" + str(time.time_ns())


class SlackSender:
    def __init__(self):
        self.token = os.getenv("SLACK_BOT_TOKEN", "")
        self.channel = os.getenv("SLACK_CHANNEL_ID", "")
        self.webhook = os.getenv("SLACK_WEBHOOK_URL", "")
        if self.token and self.channel:
            return
        if self.webhook:
            p = urllib.parse.urlsplit(self.webhook)
            if (p.scheme != "https" or p.hostname not in {"hooks.slack.com", "hooks.slack-gov.com"}
                    or not p.path.startswith("/services/")):
                raise ValueError("Use an official Slack incoming webhook URL")
            return
        raise ValueError("Set SLACK_BOT_TOKEN plus SLACK_CHANNEL_ID, or SLACK_WEBHOOK_URL")

    def send(self, text: str, thread_ts: str | None = None) -> str | None:
        if self.token and self.channel:
            body: dict[str, Any] = {"channel": self.channel, "text": text,
                                    "unfurl_links": False, "unfurl_media": False,
                                    "parse": "none"}
            if thread_ts:
                body["thread_ts"] = thread_ts
                body["reply_broadcast"] = False
            response = http_request("https://slack.com/api/chat.postMessage", payload=body,
                                    headers={"Authorization": "Bearer " + self.token})
            if not response.get("ok"):
                # Log only an allowlisted error code, never the full response.
                code = re.sub(r"[^a-z0-9_]", "", str(response.get("error", "unknown")))[:60]
                raise RemoteError("Slack rejected message: " + code)
            if not isinstance(response.get("ts"), str):
                raise RemoteError("Slack response did not contain a message timestamp")
            return response["ts"]
        response = http_request(self.webhook, payload={"text": text}, raw=True)
        if response.strip() != "ok":
            raise RemoteError("Slack webhook did not acknowledge delivery")
        return None


def send_slack_test() -> int:
    """Send exactly one explicit Slack connectivity test; never starts monitoring."""
    sender = SlackSender()
    message = (
        "*Western WA Outage Watch — Slack delivery test*\n\n"
        "This is a one-time connectivity test. It is not an outage alert, "
        "and no outage has been detected by this message.\n"
        "The monitoring setting `DRY_RUN=true` remains unchanged; this command "
        "does not start the outage-monitoring worker."
    )
    try:
        timestamp = sender.send(message)
    except (RemoteError, ValueError, OSError) as exc:
        LOG.error("Slack test failed: %s", str(exc)[:240])
        return 1
    if timestamp:
        LOG.info("Slack accepted the one-time test message (message timestamp %s)", timestamp)
    else:
        LOG.info("Slack incoming webhook accepted the one-time test message")
    return 0


def escaped(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Engine:
    def __init__(self, cfg: Config, store: StateStore, sender: Sender):
        cfg.validate()
        self.cfg, self.store, self.sender = cfg, store, sender
        self.state = store.load() or {"fingerprint": cfg.fingerprint(), "areas": {},
                                     "events": {}, "posts": [], "next_send_at": 0,
                                     "send_failures": 0, "last_digest": 0}
        if self.state["fingerprint"] != cfg.fingerprint():
            raise ValueError("Alert semantics changed. Back up the database, then use a new STATE_FILE")

    def scoped(self, snapshots: dict[str, Snapshot], now: float) -> dict[str, Snapshot]:
        if self.cfg.source_kind == "utility":
            utility_usable = {k: v for k, v in snapshots.items()
                              if k in (set(self.cfg.utility_sources) | set(self.cfg.optional_utility_sources))
                              and v.valid(now, self.cfg)}
            if utility_usable:
                usable = utility_usable
                required = set(self.cfg.utility_sources)
            else:
                # Preserve county-shaped snapshots for offline/regression tests and
                # legacy JSON inputs that still use the county schema.
                usable = {k: v for k, v in snapshots.items()
                          if k in self.cfg.counties and v.valid(now, self.cfg)}
                required = set(self.cfg.counties)
        else:
            usable = {k: v for k, v in snapshots.items()
                      if k in self.cfg.counties and v.valid(now, self.cfg)}
            required = set(self.cfg.counties)
        if self.cfg.scope == "county":
            return usable
        # Never undercount a regional total by silently dropping an unknown source.
        if not required.issubset(set(usable)):
            return {}
        stamps = [v.observed_at for v in usable.values()]
        if max(stamps) - min(stamps) > self.cfg.regional_max_skew:
            return {}
        sources = ", ".join(sorted({v.source for v in usable.values()}))
        region_name = "Western Washington (utility aggregate)" if self.cfg.source_kind == "utility" else "Western Washington (county proxy)"
        return {"region": Snapshot("region", region_name,
                                   sum(v.count for v in usable.values()), min(stamps), sources,
                                   next(iter(usable.values())).url,
                                   all(v.recovery_allowed for v in usable.values()))}

    def observe(self, snapshots: dict[str, Snapshot], now: float) -> None:
        scoped = self.scoped(snapshots, now)
        keys = list(self.cfg.counties) if self.cfg.scope == "county" else ["region"]
        for key in keys:
            area = self.state["areas"].setdefault(key, {"last_ts": None, "last_count": None,
                    "high_since": None, "low_since": None, "event_id": None, "valid": False})
            snap = scoped.get(key)
            area["valid"] = False
            if snap is None:
                area["high_since"] = area["low_since"] = None
                continue
            # Ignore old/cached snapshots and conflicting counts with the same timestamp.
            if not snap.recovery_allowed:
                area["low_since"] = None
            last = area["last_ts"]
            if last is not None and snap.observed_at <= last:
                if snap.observed_at == last and snap.count == area["last_count"]:
                    area["valid"] = True
                else:
                    area["high_since"] = area["low_since"] = None
                continue
            if last is not None and snap.observed_at - last > self.cfg.max_gap:
                area["high_since"] = area["low_since"] = None
            area.update(last_ts=snap.observed_at, last_count=snap.count, valid=True)
            if snap.count > self.cfg.threshold:
                if area["high_since"] is None:
                    area["high_since"] = snap.observed_at
            else:
                area["high_since"] = None
            if snap.count < self.cfg.reset_below and snap.recovery_allowed:
                if area["low_since"] is None:
                    area["low_since"] = snap.observed_at
            else:
                area["low_since"] = None
            if area["event_id"] is None and self.qualified(area):
                event_id = hashlib.sha256(f"{key}:{area['high_since']}".encode()).hexdigest()[:20]
                area["event_id"] = event_id
                self.state["events"][event_id] = {
                    "id": event_id, "key": key, "name": snap.name,
                    "first_high": area["high_since"], "qualified_at": snap.observed_at,
                    "count": snap.count, "peak": snap.count, "observed_at": snap.observed_at,
                    "source": snap.source, "url": snap.url, "active": True,
                    "notified": False, "last_notified_at": 0, "last_notified_count": 0,
                    "thread_ts": None, "closed_at": None, "closure_notified": False,
                }
            event = self.state["events"].get(area["event_id"])
            if event:
                event.update(count=snap.count, peak=max(event["peak"], snap.count),
                             observed_at=snap.observed_at, source=snap.source, url=snap.url)
                low = area["low_since"]
                if low is not None and snap.observed_at - low >= self.cfg.reset_seconds:
                    event.update(active=False, closed_at=snap.observed_at)
                    area["event_id"] = None
                    area["high_since"] = area["low_since"] = None
        # Retain recent closed history, but do not let the database grow forever.
        self.state["events"] = {k: e for k, e in self.state["events"].items()
                                if e["active"] or now - (e["closed_at"] or now) < 7 * 86400}
        self.store.save(self.state)

    def qualified(self, area: dict) -> bool:
        start = area["high_since"]
        return (area["valid"] and start is not None and area["last_ts"] is not None
                and area["last_ts"] - start > self.cfg.hold_seconds)

    def current(self, event: dict, now: float, require_high: bool = True) -> bool:
        area = self.state["areas"][event["key"]]
        return (event["active"] and area["event_id"] == event["id"] and area["valid"]
                and now - event["observed_at"] <= self.cfg.max_age
                and (not require_high or self.qualified(area)))

    def render(self, kind: str, events: list[dict], now: float) -> str:
        titles = {"new": "Western WA power outage alert", "escalation": "Outage significantly larger",
                  "recovery": "Outage alert rearmed - NOT an all-clear", "digest": "Western WA outage digest"}
        scope_note = ("Combined total across configured Western Washington utility sources; "
                      "not an individual outage incident." if self.cfg.scope == "regional" and self.cfg.source_kind == "utility"
                      else ("Combined total across the monitored Western Washington counties; "
                            "not an individual outage incident." if self.cfg.scope == "regional"
                            else "County totals, not individual outage incidents."))
        lines = ["*" + titles[kind] + "*", scope_note]
        for event in sorted(events, key=lambda e: e["count"], reverse=True):
            area = self.state["areas"][event["key"]]
            high = area["high_since"]
            duration = int((area["last_ts"] - high) / 60) if high is not None else None
            line = f"*{escaped(event['name'])}: {event['count']:,} customers reported out*"
            if kind != "recovery" and duration is not None and self.qualified(area):
                line += f"; above {self.cfg.threshold:,} across {duration} minutes of samples"
            if kind == "escalation":
                increase = event["count"] - event["last_notified_count"]
                line += f"\nIncrease since last alert: +{increase:,} customers."
            line += f"\nAs of {local_time(event['observed_at'])}; peak observed {event['peak']:,}."
            lines.append(line)
        if kind == "recovery":
            lines.append(f"Below {self.cfg.reset_below:,} for {self.cfg.reset_seconds // 60} minutes of samples. "
                         "Some customers may still be without power.")
        sources = sorted({e["source"] for e in events})
        lines.append("Sources: " + escaped(", ".join(sources)) + ". Counts are accounts, not people.")
        if "MassOutage" in sources:
            lines.append("<https://massoutage.com|MassOutage data> | <https://poweroutage.us/area/state/washington|Cross-check Washington outages>")
        else:
            lines.append("<" + safe_url(events[0]["url"]) + "|Source details>")
        if self.cfg.source_kind == "utility" and self.cfg.scope == "regional":
            source_names = [x.strip() for x in event["source"].split(",") if x.strip()]
            required_count = len(set(source_names) & set(self.cfg.utility_sources))
            lines.append(f"Coverage: {required_count}/{len(self.cfg.utility_sources)} required utility sources fresh; optional sources are included when available.")
            boundary_note = "Utility-source aggregate; this is not an exact west-of-the-Cascade-crest geofence."
        else:
            boundary_note = "County-based Western WA boundary; source coverage may be incomplete."
        lines.append(boundary_note + " Not an emergency service.")
        return "\n\n".join(lines)

    def deliver(self, now: float) -> list[str]:
        self.state["posts"] = [t for t in self.state["posts"] if now - t < 3600]
        if now < self.state["next_send_at"]:
            return []
        all_events = list(self.state["events"].values())
        batches: list[tuple[str, list[dict], str | None]] = []
        if self.cfg.mode == "digest":
            active = [e for e in all_events if self.current(e, now, require_high=False)]
            active = [e for e in active if e["notified"] or self.current(e, now)]
            if active and now - self.state["last_digest"] >= self.cfg.digest_seconds:
                batches.append(("digest", active, None))
        else:
            opening = [e for e in all_events if not e["notified"] and self.current(e, now)]
            if opening:
                batches.append(("new", opening, None))
            if self.cfg.mode == "balanced":
                upgrades = [e for e in all_events if e["notified"] and self.current(e, now)
                            and now - e["last_notified_at"] >= self.cfg.escalation_seconds
                            and e["count"] - e["last_notified_count"] >= self.cfg.escalation_absolute]
                for root in dict.fromkeys(e["thread_ts"] for e in upgrades):
                    batches.append(("escalation", [e for e in upgrades if e["thread_ts"] == root], root))
            if self.cfg.recovery_posts:
                closed = [e for e in all_events if not e["active"] and e["notified"]
                          and not e["closure_notified"] and now - e["closed_at"] <= self.cfg.max_age]
                for root in dict.fromkeys(e["thread_ts"] for e in closed):
                    batches.append(("recovery", [e for e in closed if e["thread_ts"] == root], root))
        sent: list[str] = []
        for kind, events, root in batches:
            if len(self.state["posts"]) >= self.cfg.max_posts_hour:
                break  # Keep eligible events pending; do not discard them.
            try:
                ts = self.sender.send(self.render(kind, events, now), root)
            except RemoteError as exc:
                failures = min(self.state["send_failures"] + 1, 10)
                self.state.update(send_failures=failures,
                                  next_send_at=now + max(exc.retry_after, min(3600, 30 * 2**failures)))
                LOG.error("Delivery failed; retry deferred: %s", exc)
                break
            self.state["posts"].append(now)
            self.state.update(next_send_at=0, send_failures=0)
            for event in events:
                if kind == "recovery":
                    event["closure_notified"] = True
                else:
                    if not event["notified"]:
                        event["thread_ts"] = ts
                    event.update(notified=True, last_notified_at=now, last_notified_count=event["count"])
            if kind == "digest":
                self.state["last_digest"] = now
            self.store.save(self.state)
            sent.append(kind)
        self.store.save(self.state)
        return sent

    def log_diagnostics(self, snapshots: dict[str, Snapshot], now: float,
                        issues: list[str] | None = None) -> str:
        """Log the regional gate and a human-readable reason for no notification."""
        issues = issues or []
        valid = {k: snap for k, snap in snapshots.items() if snap.valid(now, self.cfg)}
        scoped = self.scoped(snapshots, now)
        if self.cfg.scope == "regional":
            region = scoped.get("region")
            fresh_counties = sum(1 for key in self.cfg.counties if key in valid)
            if region is None:
                reason = "regional total unavailable (incomplete, stale, or time-skewed county data)"
                LOG.warning(
                    "Regional diagnostic: coverage=%d/%d; total=UNKNOWN; threshold=%s; "
                    "opening alert=BLOCKED; reason=%s",
                    fresh_counties, len(self.cfg.counties), f">{self.cfg.threshold:,}", reason)
                if issues:
                    LOG.info("Regional diagnostic source issues: %s", "; ".join(issues))
                return reason

            area = self.state["areas"].get("region", {})
            count = region.count
            high_since = area.get("high_since")
            last_ts = area.get("last_ts")
            duration = max(0, int(last_ts - high_since)) if high_since is not None and last_ts is not None else 0
            above = count > self.cfg.threshold
            qualified = bool(area.get("valid") and above and high_since is not None
                             and last_ts is not None and duration > self.cfg.hold_seconds)
            active = [e for e in self.state["events"].values()
                      if e.get("key") == "region" and e.get("active")]
            opening = [e for e in active if not e.get("notified") and self.current(e, now)]
            now_posts = [t for t in self.state["posts"] if now - t < 3600]
            cooldown_remaining = max(0, int(self.state.get("next_send_at", 0) - now))
            growth_wait = []
            growth_ready = []
            for event in active:
                if not event.get("notified") or not self.current(event, now):
                    continue
                increase = event["count"] - event.get("last_notified_count", 0)
                elapsed = now - event.get("last_notified_at", 0)
                if increase >= self.cfg.escalation_absolute and elapsed >= self.cfg.escalation_seconds:
                    growth_ready.append(event)
                elif increase >= self.cfg.escalation_absolute:
                    growth_wait.append(max(0, int(self.cfg.escalation_seconds - elapsed)))

            if not above:
                reason = f"combined count {count:,} is not above {self.cfg.threshold:,}"
            elif not qualified:
                remaining = max(0, self.cfg.hold_seconds + 1 - duration)
                reason = f"threshold hold not yet exceeded; observed {duration}s, needs >{self.cfg.hold_seconds}s (about {remaining}s more)"
            elif opening:
                if cooldown_remaining:
                    reason = f"opening alert eligible but delivery backoff has {cooldown_remaining}s remaining"
                elif len(now_posts) >= self.cfg.max_posts_hour:
                    reason = f"opening alert eligible but hourly cap reached ({len(now_posts)}/{self.cfg.max_posts_hour})"
                else:
                    reason = "opening alert eligible; delivery will be attempted"
            elif growth_ready:
                if cooldown_remaining:
                    reason = f"growth update eligible but delivery backoff has {cooldown_remaining}s remaining"
                elif len(now_posts) >= self.cfg.max_posts_hour:
                    reason = f"growth update eligible but hourly cap reached ({len(now_posts)}/{self.cfg.max_posts_hour})"
                else:
                    reason = "growth update eligible; delivery will be attempted"
            elif growth_wait:
                reason = f"growth increase reached +{self.cfg.escalation_absolute:,}; 30-minute cooldown has {min(growth_wait)}s remaining"
            elif active and any(e.get("notified") for e in active):
                last_count = max(e.get("last_notified_count", 0) for e in active)
                delta = count - last_count
                reason = (f"existing episode already notified; increase since last notification is {delta:+,}, "
                          f"needs +{self.cfg.escalation_absolute:,}")
            else:
                reason = "no active qualified notification event"

            LOG.info(
                "Regional diagnostic: coverage=%d/%d; total=%s; threshold=>%s; "
                "above_threshold=%s; duration_above_threshold=%ds; required=>%ds; "
                "qualified=%s; active_events=%d; hourly_posts=%d/%d; provider_degraded=%s",
                fresh_counties, len(self.cfg.counties), f"{count:,}", f"{self.cfg.threshold:,}",
                "yes" if above else "no", duration, self.cfg.hold_seconds,
                "yes" if qualified else "no", len(active), len(now_posts),
                self.cfg.max_posts_hour,
                "yes" if any(not snap.recovery_allowed for snap in snapshots.values()) else "no")
            LOG.info("Notification decision: %s", reason)
            if issues:
                LOG.info("Regional diagnostic source issues: %s", "; ".join(issues))
            return reason

        # County mode diagnostic (kept for compatibility with older configurations).
        LOG.info("County diagnostic: valid=%d/%d; active_events=%d",
                 len(valid), len(self.cfg.counties),
                 sum(1 for e in self.state["events"].values() if e.get("active")))
        return "county-mode diagnostic logged"

    def tick(self, snapshots: dict[str, Snapshot], now: float) -> list[str]:
        self.observe(snapshots, now)
        self.log_diagnostics(snapshots, now)
        sent = self.deliver(now)
        if sent:
            mode = "DRY_RUN" if isinstance(self.sender, ConsoleSender) else "SLACK"
            LOG.info("Notification action completed; mode=%s; event_types=%s",
                     mode, ",".join(sent))
        else:
            LOG.info("Notification action: no message sent on this poll; see the decision reason above")
        return sent


def demo() -> None:
    """No networking or actual Slack calls, regardless of environment settings."""
    cfg = Config(source_kind="json")
    start = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc).timestamp()
    with tempfile.TemporaryDirectory() as temp:
        store = StateStore(str(Path(temp) / "demo.db"))
        engine = Engine(cfg, store, ConsoleSender())
        for minute in range(0, 141, 5):
            # New source sample only every ten minutes, regardless of polling.
            observed = start + (minute // 10) * 600
            # Region: 12,000 -> 22,000 -> 32,000. The last rise is below 50%.
            # Increases occur before cooldown expiry, then consolidate at delivery.
            count = 6_000 if minute < 40 else (11_000 if minute < 70 else 16_000)
            data = {k: Snapshot(k, name + " County",
                               count if k in {"53033", "53061"} else 0, observed,
                               "SIMULATED - NOT LIVE", "https://example.com", True)
                    for k, name in cfg.counties.items()}
            kinds = engine.tick(data, start + minute * 60)
            print(f"Demo t={minute:3}m: " + (", ".join(kinds) if kinds else "no new Slack message"))
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Offline simulated demonstration")
    parser.add_argument("--once", action="store_true", help="One poll and exit")
    parser.add_argument("--check", action="store_true", help="Check live source without sending or saving alert state")
    parser.add_argument("--health", action="store_true", help="Exit 0 only when the worker and data are recent")
    parser.add_argument("--slack-test", action="store_true", help="Send one explicit Slack test message; does not start monitoring")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.demo:
        demo()
        return 0
    load_env()
    if args.slack_test:
        # This one-off path intentionally bypasses the worker's DRY_RUN sender
        # without changing DRY_RUN or touching alert state/source APIs.
        return send_slack_test()
    cfg = Config.from_env()
    dry_run = env_bool("DRY_RUN", True)
    if not dry_run and not args.check and not args.health and not env_bool("ACK_COUNTY_PROXY", False):
        raise ValueError("Set ACK_COUNTY_PROXY=true after reviewing the geographic and coverage limitations")
    state_file = os.getenv("STATE_FILE", "data/state.dry-run.db" if dry_run else "data/state.live.db")
    health_path = Path(state_file + ".health.json")
    if args.health:
        try:
            value = json.loads(health_path.read_text())
            now = time.time()
            good = (now - value["checked_at"] < max(900, cfg.poll_seconds * 3)
                    and value["usable_counties"] == len(cfg.counties)
                    and value["regional_usable"] and not value["delivery_degraded"]
                    and not value["source_issues"])
            return 0 if good else 1
        except (OSError, KeyError, ValueError):
            return 1
    provider_name = cfg.source_kind
    if provider_name == "massoutage":
        provider: Any = MassOutageProvider(cfg)
    elif provider_name == "json":
        provider = JsonProvider(cfg, os.environ["OUTAGE_JSON"])
    elif provider_name == "utility":
        provider = UtilityAggregateProvider(cfg)
    else:
        raise ValueError("OUTAGE_SOURCE must be massoutage, json, or utility")
    store = None if args.check else StateStore(state_file)
    sender: Sender = ConsoleSender() if dry_run or args.check else SlackSender()
    engine = None if store is None else Engine(cfg, store, sender)
    if engine is not None:
        mode = "dry-run" if dry_run else "slack"
        target = "console" if dry_run else (os.getenv("SLACK_CHANNEL_ID") or os.getenv("SLACK_WEBHOOK_URL", ""))
        context = mode + ":" + hashlib.sha256(target.encode()).hexdigest()
        if engine.state.get("delivery_context", context) != context:
            raise ValueError("Delivery target changed. Use a separate STATE_FILE for each destination and dry-run/live mode")
        engine.state["delivery_context"] = context
        engine.store.save(engine.state)
    if cfg.source_kind == "utility":
        LOG.info("Mode=%s, scope=%s, delivery=%s; %d utility sources", cfg.mode, cfg.scope,
                 "DRY RUN" if dry_run else "SLACK", len(cfg.utility_sources))
    else:
        LOG.info("Mode=%s, scope=%s, delivery=%s; %d county proxies", cfg.mode, cfg.scope,
                 "DRY RUN" if dry_run else "SLACK", len(cfg.counties))
    try:
        while True:
            t0 = time.monotonic()
            try:
                snapshots, issues = provider.fetch()
            except (RemoteError, OSError, ValueError, KeyError, TypeError) as exc:
                snapshots, issues = {}, [f"Source unavailable ({type(exc).__name__})"]
                provider.retry_after = getattr(exc, "retry_after", cfg.poll_seconds)
            now = time.time()
            valid = {k: s for k, s in snapshots.items() if s.valid(now, cfg)}
            for issue in issues:
                LOG.warning("%s", issue)
            if cfg.source_kind == "utility":
                required_fresh = len(set(valid) & set(cfg.utility_sources))
                optional_fresh = len(set(valid) & set(cfg.optional_utility_sources))
                LOG.info("Fresh utility sources: %d/%d required; optional=%d", required_fresh, len(cfg.utility_sources), optional_fresh)
            else:
                LOG.info("Fresh counties: %d/%d; missing is UNKNOWN, not zero", len(valid), len(cfg.counties))
            if args.check:
                if cfg.source_kind == "utility":
                    for key in cfg.utility_sources:
                        s = valid.get(key)
                        if s:
                            print(f"{s.name}: OK; {s.count:,} customers as of {local_time(s.observed_at)}")
                        else:
                            print(f"{key.upper()}: UNKNOWN")
                    ok = set(valid) == set(cfg.utility_sources) and not issues
                else:
                    for k in cfg.counties:
                        s = valid.get(k)
                        print(f"{cfg.counties[k]}: {s.count if s else 'UNKNOWN'}" +
                              (f" as of {local_time(s.observed_at)}" if s else ""))
                    # A global feed-health warning may refer to feeds outside
                    # Washington. The county snapshots are the regional gate;
                    # each snapshot has its own current status and as_of time.
                    ok = len(valid) == len(cfg.counties)
                return 0 if ok else 1
            assert engine is not None
            engine.observe(snapshots, now)
            if args.once:
                engine.log_diagnostics(snapshots, now, issues)
            sent = engine.deliver(now)
            if sent:
                LOG.info("Notification action completed; mode=%s; event_types=%s",
                         "DRY_RUN" if dry_run else "SLACK", ",".join(sent))
            else:
                LOG.info("Notification action: no message sent on this poll; see the decision reason above")
            health_path.parent.mkdir(parents=True, exist_ok=True)
            health = {"checked_at": now, "usable_counties": len(valid), "source_issues": len(issues),
                      "regional_usable": bool(engine.scoped(snapshots, now)),
                      "delivery_degraded": engine.state["send_failures"] > 0,
                      "dry_run": dry_run}
            temp = health_path.with_suffix(".tmp")
            temp.write_text(json.dumps(health), encoding="utf-8")
            temp.replace(health_path)
            if args.once:
                return 0
            delay = max(cfg.poll_seconds - (time.monotonic() - t0), getattr(provider, "retry_after", 0), 1)
            time.sleep(delay)
    except KeyboardInterrupt:
        return 0
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, sqlite3.Error, OSError) as exc:
        # Configuration errors are safe to show; raw network exceptions are sanitized above.
        LOG.error("Configuration/state error: %s", exc)
        sys.exit(2)
