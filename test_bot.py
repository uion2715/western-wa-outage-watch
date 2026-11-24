import os
"""Offline contract/behavior tests. They do not certify upstream API availability."""
import json
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from bot import (COUNTIES, Config, ConsoleSender, Engine, JsonProvider, MassOutageProvider, MassOutageUtilityProvider, PSEProvider, PowerOutageTrackerUtilityProvider, UtilityAggregateProvider,
                 RemoteError, SlackSender, Snapshot, StateStore, iso, nonnegative_int,
                 parse_time, safe_url, load_env)

BASE = 1_790_000_000.0


class RecordingSender:
    def __init__(self):
        self.messages = []
        self.failures = 0

    def send(self, text, thread_ts=None):
        if self.failures:
            self.failures -= 1
            raise RemoteError("simulated delivery failure", 120)
        self.messages.append((text, thread_ts))
        return str(len(self.messages)) + ".000001"


class UtilityAdapterTests(unittest.TestCase):
    def test_poweroutagetracker_scl_api_parser(self):
        stamp = iso(time.time())
        payload = {"state": {"last_updated": stamp, "utilities": [{"name": "Seattle City Light", "customers_out": 249, "last_updated": stamp}]}}
        with patch("bot.http_request", return_value=payload):
            rows, issues = PowerOutageTrackerUtilityProvider(Config(), "scl").fetch()
        self.assertEqual(issues, [])
        self.assertEqual(rows["scl"].count, 249)

    def test_poweroutagetracker_snopud_api_parser(self):
        stamp = iso(time.time())
        payload = {"state": {"last_updated": stamp, "utilities": [{"name": "PUD No 1 of Snohomish County - (WA)", "customers_out": 8, "last_updated": stamp}]}}
        with patch("bot.http_request", return_value=payload):
            rows, issues = PowerOutageTrackerUtilityProvider(Config(), "snopud").fetch()
        self.assertEqual(issues, [])
        self.assertEqual(rows["snopud"].count, 8)

    def test_poweroutagetracker_api_fails_closed_without_fresh_timestamp(self):
        payload = {"state": {"utilities": [{"name": "Seattle City Light", "customers_out": 8}]}}
        with patch("bot.http_request", return_value=payload):
            rows, issues = PowerOutageTrackerUtilityProvider(Config(), "scl").fetch()
        self.assertEqual(rows, {})
        self.assertTrue(issues)

    def test_pse_json_parser_extracts_incidents(self):
        body = json.dumps({
            "UnplannedOutageSummary": {"CustomerAfftectedCount": 12000, "OutageCount": 1, "LastUpdatedDatetime": "11/10 02:14 PM"},
            "PseMap": [{"DataProvider": {"Attributes": [
                {"RefName": "StartDate", "Value": "11/10 01:55 PM"},
                {"RefName": "Customers impacted", "Value": "12000"},
                {"RefName": "LastUpdated", "Value": "11/10 02:14 PM"},
                {"RefName": "OutageEventId", "Value": "INC123"},
            ], "PointOfInterest": {"Id": "INC123", "PlannedOutage": False}}}]
        })
        rows = PSEProvider.parse_body(body, time.time())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "INC123")
        self.assertEqual(rows[0]["count"], 12000)

    def test_pse_parser_fails_closed_on_unrelated_json(self):
        self.assertEqual(PSEProvider.parse_body('{"hello":"world"}', BASE), [])


    def test_utility_scope_uses_all_enabled_sources(self):
        cfg = Config(source_kind="utility", utility_sources=("pse",))
        store = StateStore(str(Path(tempfile.mkdtemp()) / "state.db"))
        sender = RecordingSender()
        engine = Engine(cfg, store, sender)
        snaps = {"pse": Snapshot("pse", "Puget Sound Energy", 12001, BASE, "PSE", "https://www.pse.com") }
        engine.tick(snaps, BASE)
        engine.tick({"pse": Snapshot("pse", "Puget Sound Energy", 12001, BASE+600, "PSE", "https://www.pse.com")}, BASE+600)
        engine.tick({"pse": Snapshot("pse", "Puget Sound Energy", 12001, BASE+1200, "PSE", "https://www.pse.com")}, BASE+1200)
        engine.tick({"pse": Snapshot("pse", "Puget Sound Energy", 12001, BASE+1800, "PSE", "https://www.pse.com")}, BASE+1800)
        self.assertEqual(len(sender.messages), 1)
        store.close()



    def test_optional_utility_does_not_block_regional_total(self):
        cfg = Config(source_kind="utility", utility_sources=("pse", "scl", "snopud"), optional_utility_sources=("tacoma",), max_age=10**9)
        store = StateStore(str(Path(tempfile.mkdtemp()) / "state.db"))
        sender = RecordingSender()
        engine = Engine(cfg, store, sender)
        snaps = {
            "pse": Snapshot("pse", "Puget Sound Energy", 4000, BASE, "PSE", "https://www.pse.com/en/outage/outage-map"),
            "scl": Snapshot("scl", "Seattle City Light", 3000, BASE, "PowerOutageTracker", "https://poweroutagetracker.com/utilities/seattle-city-light"),
            "snopud": Snapshot("snopud", "Snohomish County PUD", 4000, BASE, "PowerOutageTracker", "https://poweroutagetracker.com/utilities/snohomish-county-pud"),
        }
        engine.tick(snaps, BASE)
        engine.tick({k: replace(v, observed_at=BASE+600) for k,v in snaps.items()}, BASE+600)
        engine.tick({k: replace(v, observed_at=BASE+1200) for k,v in snaps.items()}, BASE+1200)
        self.assertEqual(len(sender.messages), 1)
        store.close()

    def test_optional_utility_is_included_when_fresh(self):
        cfg = Config(source_kind="utility", utility_sources=("pse", "scl", "snopud"), optional_utility_sources=("tacoma",), max_age=10**9)
        store = StateStore(str(Path(tempfile.mkdtemp()) / "state.db"))
        sender = RecordingSender()
        engine = Engine(cfg, store, sender)
        snaps = {
            "pse": Snapshot("pse", "Puget Sound Energy", 4000, BASE, "PSE", "https://www.pse.com/en/outage/outage-map"),
            "scl": Snapshot("scl", "Seattle City Light", 3000, BASE, "PowerOutageTracker", "https://poweroutagetracker.com/utilities/seattle-city-light"),
            "snopud": Snapshot("snopud", "Snohomish County PUD", 4000, BASE, "PowerOutageTracker", "https://poweroutagetracker.com/utilities/snohomish-county-pud"),
            "tacoma": Snapshot("tacoma", "Tacoma Power", 1, BASE, "Tacoma Power", "https://www.mytpu.org/outages-safety/power-outages/")
        }
        engine.tick(snaps, BASE)
        engine.tick({k: replace(v, observed_at=BASE+600) for k,v in snaps.items()}, BASE+600)
        engine.tick({k: replace(v, observed_at=BASE+1200) for k,v in snaps.items()}, BASE+1200)
        self.assertIn("11,001", sender.messages[0][0])
        self.assertIn("optional sources are included when available", sender.messages[0][0])
        store.close()

    def test_live_utility_scope_requires_all_configured_utility_sources(self):
        cfg = Config(source_kind="utility", utility_sources=("pse", "scl", "snopud", "tacoma"))
        store = StateStore(str(Path(tempfile.mkdtemp()) / "state.db"))
        sender = RecordingSender()
        engine = Engine(cfg, store, sender)
        complete = {
            "pse": Snapshot("pse", "Puget Sound Energy", 3000, BASE, "PSE", "https://www.pse.com"),
            "scl": Snapshot("scl", "Seattle City Light", 2000, BASE, "SCL", "https://www.seattle.gov/city-light"),
            "snopud": Snapshot("snopud", "Snohomish County PUD", 1000, BASE, "SNOPUD", "https://www.snopud.com"),
            "tacoma": Snapshot("tacoma", "Tacoma Power", 500, BASE, "Tacoma Power", "https://www.mytpu.org"),
        }
        self.assertIn("region", engine.scoped(complete, BASE))
        self.assertEqual(engine.scoped({k: v for k, v in complete.items() if k != "scl"}, BASE), {})
        store.close()

    def test_pse_timestamp_fallback_works_without_tzdata(self):
        original = PSEProvider._pacific_tz_for_date
        with patch.object(PSEProvider, "_pacific_tz_for_date", side_effect=lambda dt: timezone(timedelta(hours=-8), name="PST")):
            now = datetime(2026, 11, 10, 14, 30, tzinfo=timezone(timedelta(hours=-8))).timestamp()
            ts = PSEProvider._parse_local_stamp("11/10 02:14 PM", now)
            self.assertEqual(ts, now - 16 * 60)
        PSEProvider._pacific_tz_for_date = original

    def test_pse_timestamps_are_parsed_as_pacific_time(self):
        from datetime import datetime, timezone, timedelta
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
        now = datetime(2026, 11, 10, 14, 30, tzinfo=tz).timestamp()
        ts = PSEProvider._parse_local_stamp("11/10 02:14 PM", now)
        expected = datetime(2026, 11, 10, 14, 14, tzinfo=tz).timestamp()
        self.assertEqual(ts, expected)
        self.assertLess(now - ts, 20 * 60)

    def test_pse_current_feed_shape(self):
        payload = {
            "UnplannedOutageSummary": {"CustomerAfftectedCount": 701, "OutageCount": 17, "LastUpdatedDatetime": "11/10 02:14 PM"},
            "PseMap": [
                {"DataProvider": {"Attributes": [
                    {"RefName": "StartDate", "Value": "11/10 09:57 AM"},
                    {"RefName": "Customers impacted", "Value": "1"},
                    {"RefName": "LastUpdated", "Value": "11/10 02:10 PM"},
                    {"RefName": "OutageEventId", "Value": "INC1140139508"},
                ], "PointOfInterest": {"Id": "INC1140139508", "PlannedOutage": False}}},
                {"DataProvider": {"Attributes": [
                    {"RefName": "StartDate", "Value": "11/10 08:52 AM"},
                    {"RefName": "Customers impacted", "Value": "6"},
                    {"RefName": "LastUpdated", "Value": "11/10 02:10 PM"},
                    {"RefName": "OutageEventId", "Value": "INC1140139484"},
                ], "PointOfInterest": {"Id": "INC1140139484", "PlannedOutage": True, "FilterType": "Planned"}}},
            ]
        }
        rows = PSEProvider.parse_body(payload, time.time())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "INC1140139508")
        self.assertEqual(rows[0]["count"], 1)

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # Keep the original county-mode regression suite explicit.
        self.cfg = Config(scope="county", counties={"53033": "King", "53061": "Snohomish"})
        self.sender = RecordingSender()
        self.store = StateStore(str(Path(self.temp.name) / "test.db"))
        self.engine = Engine(self.cfg, self.store, self.sender)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def use_config(self, **kwargs):
        self.store.close()
        self.cfg = replace(self.cfg, **kwargs)
        self.store = StateStore(str(Path(self.temp.name) / (str(len(list(Path(self.temp.name).iterdir()))) + ".db")))
        self.engine = Engine(self.cfg, self.store, self.sender)

    def sample(self, minute, king=12000, snohomish=0, observed=None, recovery=True):
        ts = BASE + (minute if observed is None else observed) * 60
        return {key: Snapshot(key, self.cfg.counties[key], count, ts,
                              "Test source", "https://example.com", recovery)
                for key, count in (("53033", king), ("53061", snohomish))}

    def tick(self, minute, **kwargs):
        return self.engine.tick(self.sample(minute, **kwargs), BASE + minute * 60)

    def qualify(self, king=12000, snohomish=0):
        for minute in (0, 10, 20):
            self.tick(minute, king=king, snohomish=snohomish)

    def test_strict_customer_threshold(self):
        for m in (0, 10, 20, 30):
            self.tick(m, king=10000)
        self.assertEqual(self.sender.messages, [])

    def test_strict_longer_than_fifteen_minutes(self):
        for m in (0, 5, 10, 15):
            self.tick(m)
        self.assertEqual(self.sender.messages, [])
        self.assertEqual(self.tick(16), ["new"])

    def test_cached_data_does_not_advance_duration(self):
        for m in (0, 5, 10, 15, 20):
            self.tick(m, observed=0)
        self.assertEqual(self.sender.messages, [])

    def test_ten_minute_source_opens_at_twenty(self):
        for m in (0, 5, 10, 15):
            self.tick(m, observed=(m // 10) * 10)
        self.assertEqual(self.sender.messages, [])
        self.assertEqual(self.tick(20), ["new"])

    def test_short_spike_resets_timer(self):
        self.tick(0)
        self.tick(10, king=9000)
        self.tick(20)
        self.tick(30)
        self.assertEqual(self.sender.messages, [])
        self.tick(40)
        self.assertEqual(len(self.sender.messages), 1)

    def test_simultaneous_counties_batched(self):
        self.qualify(snohomish=15000)
        self.assertEqual(len(self.sender.messages), 1)
        self.assertIn("King", self.sender.messages[0][0])
        self.assertIn("Snohomish", self.sender.messages[0][0])

    def test_normal_repeats_suppressed(self):
        for m in range(0, 201, 10):
            self.tick(m)
        self.assertEqual(len(self.sender.messages), 1)

    def test_restart_remembers_notification(self):
        self.qualify()
        self.engine = Engine(self.cfg, self.store, self.sender)
        self.tick(30)
        self.assertEqual(len(self.sender.messages), 1)

    def test_threshold_flapping_does_not_realert(self):
        self.qualify()
        self.tick(30, king=9000)
        for m in (40, 50, 60):
            self.tick(m)
        self.assertEqual(len(self.sender.messages), 1)

    def test_rearm_requires_sustained_lower_threshold(self):
        self.qualify()
        for m in (30, 40, 50):
            self.tick(m, king=4000)
        self.assertTrue(next(iter(self.engine.state["events"].values()))["active"])
        self.tick(60, king=4000)
        self.assertFalse(next(iter(self.engine.state["events"].values()))["active"])
        for m in (70, 80, 90):
            self.tick(m)
        self.assertEqual(len(self.sender.messages), 2)

    def test_missing_data_not_recovery(self):
        self.qualify()
        for m in (30, 40, 50, 60, 70):
            self.engine.tick({}, BASE + m * 60)
        self.assertTrue(next(iter(self.engine.state["events"].values()))["active"])
        self.assertEqual(len(self.sender.messages), 1)

    def test_data_gap_resets_unconfirmed_duration(self):
        self.tick(0)
        self.engine.tick({}, BASE + 10 * 60)
        self.tick(20)
        self.tick(30)
        self.assertEqual(self.sender.messages, [])
        self.tick(40)
        self.assertEqual(len(self.sender.messages), 1)

    def test_long_sampling_gap_resets_duration(self):
        self.tick(0)
        self.tick(20)
        self.assertEqual(self.sender.messages, [])
        self.tick(30)
        self.tick(40)
        self.assertEqual(len(self.sender.messages), 1)

    def test_stale_and_future_data_rejected(self):
        self.tick(30, observed=0)
        self.tick(40, observed=100)
        self.assertEqual(self.sender.messages, [])
        self.assertFalse(self.engine.state["areas"]["53033"]["valid"])

    def test_same_timestamp_with_new_count_is_uncertain(self):
        self.tick(0)
        self.tick(10)
        self.tick(15, observed=10, king=14000)
        self.assertFalse(self.engine.state["areas"]["53033"]["valid"])
        self.tick(20)
        self.assertEqual(self.sender.messages, [])

    def test_degraded_feed_never_rearms(self):
        self.qualify()
        for m in (30, 40, 50, 60, 70):
            self.tick(m, king=0, recovery=False)
        self.assertTrue(next(iter(self.engine.state["events"].values()))["active"])

    def test_escalation_requires_cooldown_and_large_increase(self):
        self.qualify()
        for m in (30, 40):
            self.tick(m, king=30000)
        self.assertEqual(len(self.sender.messages), 1)
        self.tick(50, king=30000)
        self.assertEqual(len(self.sender.messages), 2)
        self.assertEqual(self.sender.messages[1][1], "1.000001")

    def test_small_change_does_not_send_update(self):
        self.qualify()
        for m in range(30, 151, 10):
            self.tick(m, king=18000)
        self.assertEqual(len(self.sender.messages), 1)

    def test_quiet_never_escalates(self):
        self.use_config(mode="quiet")
        self.qualify()
        for m in range(30, 151, 10):
            self.tick(m, king=100000)
        self.assertEqual(len(self.sender.messages), 1)

    def test_post_cap_defers_new_still_active_area(self):
        self.use_config(max_posts_hour=1)
        self.qualify()
        for m in range(30, 80, 10):
            self.tick(m, snohomish=15000)
        self.assertEqual(len(self.sender.messages), 1)
        self.tick(80, snohomish=15000)
        self.assertEqual(len(self.sender.messages), 2)
        self.assertIn("Snohomish", self.sender.messages[1][0])

    def test_failed_slack_does_not_mark_sent(self):
        self.sender.failures = 1
        self.qualify()
        self.assertEqual(len(self.sender.messages), 0)
        event = next(iter(self.engine.state["events"].values()))
        self.assertFalse(event["notified"])
        self.tick(21, observed=20)
        self.assertEqual(len(self.sender.messages), 0)
        self.tick(25, observed=20)
        self.assertEqual(len(self.sender.messages), 1)

    def test_delayed_opening_not_sent_after_drop(self):
        self.sender.failures = 1
        self.qualify()
        self.tick(30, king=4000)
        self.assertEqual(len(self.sender.messages), 0)

    def test_region_does_not_use_partial_total(self):
        self.use_config(scope="regional")
        for m in (0, 10, 20, 30):
            data = self.sample(m, king=6000, snohomish=6000)
            del data["53061"]
            self.engine.tick(data, BASE + m * 60)
        self.assertEqual(len(self.sender.messages), 0)

    def test_region_qualifies_combined_total(self):
        self.use_config(scope="regional")
        self.qualify(king=6000, snohomish=6000)
        self.assertEqual(len(self.sender.messages), 1)
        self.assertIn("12,000", self.sender.messages[0][0])

    def test_region_rejects_mismatched_observation_times(self):
        self.use_config(scope="regional")
        data = self.sample(20)
        data["53061"] = replace(data["53061"], observed_at=BASE)
        self.assertEqual(self.engine.scoped(data, BASE + 20 * 60), {})

    def test_digest_at_most_hourly(self):
        self.use_config(mode="digest")
        for m in range(0, 141, 10):
            self.tick(m)
        self.assertEqual(len(self.sender.messages), 3)
        self.assertTrue(all("digest" in text for text, _ in self.sender.messages))

    def test_optional_recovery_is_not_all_clear(self):
        self.use_config(recovery_posts=True)
        self.qualify()
        for m in (30, 40, 50, 60):
            self.tick(m, king=4000)
        self.assertEqual(len(self.sender.messages), 2)
        self.assertIn("NOT an all-clear", self.sender.messages[1][0])

    def test_configuration_change_requires_new_state(self):
        self.qualify()
        with self.assertRaisesRegex(ValueError, "semantics changed"):
            Engine(replace(self.cfg, threshold=20000), self.store, self.sender)



class RegionalDefaultsTests(unittest.TestCase):
    """Exercise the selected defaults with fresh data for all 19 counties."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = Config()
        self.sender = RecordingSender()
        self.store = StateStore(str(Path(self.temp.name) / "regional.db"))
        self.engine = Engine(self.cfg, self.store, self.sender)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def sample(self, minute, king=6000, snohomish=6000):
        counts = {"53033": king, "53061": snohomish}
        return {key: Snapshot(key, name + " County", counts.get(key, 0),
                              BASE + minute * 60, "Test source",
                              "https://example.com", True)
                for key, name in self.cfg.counties.items()}

    def tick(self, minute, **counts):
        return self.engine.tick(self.sample(minute, **counts), BASE + minute * 60)

    def qualify(self, **counts):
        for minute in (0, 10, 20):
            self.tick(minute, **counts)

    def test_defaults_match_selected_policy(self):
        self.assertEqual(self.cfg.scope, "regional")
        self.assertEqual(self.cfg.mode, "balanced")
        self.assertEqual(self.cfg.threshold, 10000)
        self.assertEqual(self.cfg.hold_seconds, 900)
        self.assertEqual(self.cfg.escalation_absolute, 10000)
        self.assertEqual(self.cfg.escalation_seconds, 1800)
        self.assertEqual(self.cfg.max_posts_hour, 2)
        self.assertEqual(len(self.cfg.counties), 19)
        self.assertFalse(self.cfg.recovery_posts)
        self.assertFalse(hasattr(self.cfg, "escalation_fraction"))

    def test_env_example_has_selected_policy_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            load_env(str(Path(__file__).with_name(".env.example")))
            cfg = Config.from_env()
            self.assertEqual(cfg.source_kind, "massoutage")
            self.assertEqual(cfg.scope, "regional")
            self.assertEqual(cfg.counties, COUNTIES)
            self.assertEqual(cfg.escalation_seconds, 1800)
            self.assertEqual(cfg.poll_seconds, 300)
            self.assertEqual(cfg.max_age, 1800)

    def test_regional_growth_waits_for_full_thirty_minutes(self):
        self.qualify(king=50000, snohomish=50000)
        for minute in (30, 40, 49 + 59 / 60):
            self.assertEqual(self.tick(minute, king=55000, snohomish=55000), [])
        self.assertEqual(len(self.sender.messages), 1)
        self.assertEqual(self.tick(50, king=55000, snohomish=55000), ["escalation"])
        self.assertEqual(self.sender.messages[1][1], "1.000001")

    def test_repeated_updates_reset_cooldown_and_respect_rolling_cap(self):
        self.qualify(king=50000, snohomish=50000)
        for delivery_minute, count in ((50, 55000), (80, 60000), (110, 65000)):
            for minute in (delivery_minute - 20, delivery_minute - 10,
                           delivery_minute - 1 / 60):
                self.assertEqual(self.tick(minute, king=count, snohomish=count), [])
            self.assertEqual(self.tick(delivery_minute, king=count, snohomish=count),
                             ["escalation"])
            self.assertLessEqual(len(self.engine.state["posts"]), 2)
        self.assertEqual(len(self.sender.messages), 4)
        self.assertTrue(all(thread == "1.000001" for _, thread in self.sender.messages[1:]))

    def test_shortening_cooldown_preserves_existing_episode(self):
        old_cfg = replace(self.cfg, escalation_seconds=3600)
        self.engine = Engine(old_cfg, self.store, self.sender)
        self.qualify(king=50000, snohomish=50000)
        for minute in (30, 40):
            self.assertEqual(self.tick(minute, king=55000, snohomish=55000), [])
        event_id = next(iter(self.engine.state["events"]))
        self.engine = Engine(self.cfg, self.store, self.sender)
        self.assertEqual(self.tick(50, king=55000, snohomish=55000), ["escalation"])
        self.assertEqual(list(self.engine.state["events"]), [event_id])
        self.assertEqual(len(self.sender.messages), 2)
        self.assertEqual(self.sender.messages[1][1], "1.000001")

    def test_explicit_cooldown_env_setting_is_respected(self):
        for value in (1800, 3600):
            with self.subTest(seconds=value):
                with patch.dict("os.environ", {"ESCALATION_COOLDOWN_SECONDS": str(value)},
                                clear=True):
                    self.assertEqual(Config.from_env().escalation_seconds, value)

    def test_legacy_percentage_setting_does_not_restore_rule(self):
        with patch.dict("os.environ", {"ESCALATION_FRACTION": "0.5"}, clear=True):
            with self.assertLogs("outage-bot", level="WARNING") as logs:
                cfg = Config.from_env()
        self.assertEqual(cfg, Config())
        self.assertIn("no longer used", logs.output[0])

    def test_combined_count_qualifies_without_single_county_qualifying(self):
        self.qualify()
        self.assertEqual(len(self.sender.messages), 1)
        self.assertEqual(list(self.engine.state["areas"]), ["region"])
        self.assertEqual(len(self.engine.state["events"]), 1)
        text, thread = self.sender.messages[0]
        self.assertIn("12,000", text)
        self.assertIn("Combined total", text)
        self.assertIsNone(thread)

    def test_exactly_ten_thousand_regionwide_does_not_qualify(self):
        for minute in (0, 10, 20, 30):
            self.tick(minute, king=6000, snohomish=4000)
        self.assertEqual(self.sender.messages, [])

    def test_ten_thousand_and_one_regionwide_does_qualify(self):
        self.qualify(king=6000, snohomish=4001)
        self.assertEqual(len(self.sender.messages), 1)
        self.assertIn("10,001", self.sender.messages[0][0])

    def test_region_requires_more_than_fifteen_minutes(self):
        for minute in (0, 5, 10, 15):
            self.tick(minute)
        self.assertEqual(self.sender.messages, [])
        self.assertEqual(self.tick(16), ["new"])

    def test_exact_ten_thousand_rise_below_fifty_percent_sends(self):
        self.qualify(king=50000, snohomish=50000)
        for minute in (30, 40):
            self.tick(minute, king=55000, snohomish=55000)
        self.assertEqual(len(self.sender.messages), 1)
        self.assertEqual(self.tick(50, king=55000, snohomish=55000), ["escalation"])
        self.assertIn("110,000", self.sender.messages[1][0])
        self.assertIn("+10,000", self.sender.messages[1][0])
        self.assertEqual(self.sender.messages[1][1], "1.000001")

    def test_nine_thousand_nine_hundred_ninety_nine_rise_does_not_send(self):
        self.qualify(king=50000, snohomish=50000)
        for minute in range(30, 151, 10):
            self.tick(minute, king=55000, snohomish=54999)
        self.assertEqual(len(self.sender.messages), 1)

    def test_growth_baseline_is_last_notification_not_last_poll(self):
        self.qualify(king=50000, snohomish=50000)
        for minute in (30, 40):
            self.tick(minute, king=55000, snohomish=55000)
        self.assertEqual(self.tick(50, king=55000, snohomish=55000), ["escalation"])
        for minute in range(60, 141, 10):
            self.tick(minute, king=60000, snohomish=59999)
        self.assertEqual(len(self.sender.messages), 2)
        self.assertEqual(self.tick(150, king=60000, snohomish=60000), ["escalation"])
        self.assertIn("120,000", self.sender.messages[2][0])
        self.assertEqual(self.sender.messages[2][1], "1.000001")

    def test_large_jump_is_consolidated_into_one_update(self):
        self.qualify()
        for minute in range(30, 81, 10):
            self.tick(minute, king=40000, snohomish=40000)
        self.assertEqual(len(self.sender.messages), 2)
        for minute in range(90, 201, 10):
            self.tick(minute, king=40000, snohomish=40000)
        self.assertEqual(len(self.sender.messages), 2)

    def test_temporary_growth_during_cooldown_does_not_queue_obsolete_update(self):
        self.qualify(king=50000, snohomish=50000)
        for minute in (30, 40):
            self.tick(minute, king=55000, snohomish=55000)
        for minute in range(50, 151, 10):
            self.tick(minute, king=50000, snohomish=50000)
        self.assertEqual(len(self.sender.messages), 1)

    def test_county_increase_offset_elsewhere_does_not_send(self):
        self.qualify(king=6000, snohomish=6000)
        for minute in range(30, 151, 10):
            self.tick(minute, king=18000, snohomish=0)
        self.assertEqual(len(self.sender.messages), 1)

    def test_missing_county_never_becomes_zero_in_regional_total(self):
        for minute in (0, 10, 20, 30):
            data = self.sample(minute, king=15000)
            del data["53073"]
            self.engine.tick(data, BASE + minute * 60)
        self.assertEqual(self.sender.messages, [])
        self.assertFalse(self.engine.state["areas"]["region"]["valid"])

    def test_restart_preserves_regional_growth_baseline(self):
        self.qualify(king=50000, snohomish=50000)
        for minute in range(30, 81, 10):
            self.tick(minute, king=55000, snohomish=55000)
        self.assertEqual(len(self.sender.messages), 2)
        self.engine = Engine(self.cfg, self.store, self.sender)
        for minute in range(90, 151, 10):
            self.tick(minute, king=55000, snohomish=55000)
        self.assertEqual(len(self.sender.messages), 2)

    def test_regional_rearm_requires_combined_total_below_five_thousand(self):
        self.qualify()
        for minute in (30, 40, 50, 60):
            self.tick(minute, king=2000, snohomish=4000)
        event = next(iter(self.engine.state["events"].values()))
        self.assertTrue(event["active"])
        for minute in (70, 80, 90, 100):
            self.tick(minute, king=2000, snohomish=2000)
        self.assertFalse(event["active"])
        self.assertEqual(len(self.sender.messages), 1)
        for minute in (110, 120, 130):
            self.tick(minute)
        self.assertEqual(len(self.sender.messages), 2)

    def test_changing_old_county_state_to_regional_requires_fresh_state(self):
        self.qualify()
        with self.assertRaisesRegex(ValueError, "semantics changed"):
            Engine(replace(self.cfg, scope="county"), self.store, self.sender)


class AdapterTests(unittest.TestCase):
    def payload(self, count=12000):
        return {"data": {"fips": "53033", "customers_out": count,
                         "updated_at": iso(BASE + 600), "as_of": iso(BASE)},
                "meta": {"live": {"status": "ok", "last_poll_ts": iso(BASE + 600)}}}

    def test_massoutage_free_key_is_sent_as_bearer_header(self):
        with patch.dict("os.environ", {"MASSOUTAGE_API_KEY": "mo_live_test_secret"}, clear=True):
            provider = MassOutageProvider(Config(source_kind="massoutage"))
        self.assertEqual(provider.headers.get("Authorization"), "Bearer mo_live_test_secret")

    def test_massoutage_requires_key_for_nineteen_counties(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "MASSOUTAGE_API_KEY"):
                MassOutageProvider(Config(source_kind="massoutage"))

    def test_rollup_poll_time_advances_sample_but_oldest_source_controls_freshness(self):
        snap = MassOutageProvider.decode("53033", self.payload())
        self.assertEqual(snap.observed_at, BASE + 600)
        self.assertEqual(snap.freshness_at, BASE)
        self.assertTrue(snap.valid(BASE + 900, Config(max_age=1200)))
        self.assertFalse(snap.valid(BASE + 1801, Config(max_age=1200)))

    def test_zero_needs_explicit_poll_evidence(self):
        p = self.payload(0)
        p["data"]["updated_at"] = p["data"]["as_of"] = None
        self.assertEqual(MassOutageProvider.decode("53033", p).observed_at, BASE + 600)
        del p["meta"]
        with self.assertRaises(ValueError):
            MassOutageProvider.decode("53033", p)

    def test_massoutage_rollup_updates_advance_regional_hold_timer(self):
        cfg = Config(scope="regional", counties={"53033": "King", "53061": "Snohomish"},
                     source_kind="massoutage", max_age=1800, regional_max_skew=300)
        temp = tempfile.TemporaryDirectory()
        store = StateStore(str(Path(temp.name) / "state.db"))
        sender = RecordingSender()
        engine = Engine(cfg, store, sender)

        def snapshots(minute):
            updated = iso(BASE + minute * 60)
            as_of = iso(BASE - 300)  # Oldest contributing fetch is carried forward.
            rows = {}
            for fips, count in (("53033", 12001), ("53061", 0)):
                payload = {"data": {"fips": fips, "customers_out": count,
                                    "updated_at": updated, "as_of": as_of},
                           "meta": {"live": {"status": "ok", "last_poll_ts": updated}}}
                rows[fips] = MassOutageProvider.decode(fips, payload)
            return rows

        try:
            for minute in (0, 10, 20):
                engine.tick(snapshots(minute), BASE + minute * 60)
            self.assertEqual(len(sender.messages), 1)
            self.assertIn("12,001", sender.messages[0][0])
        finally:
            store.close()
            temp.cleanup()

    def test_delayed_provider_rejected(self):
        p = self.payload()
        p["meta"]["live"]["status"] = "delayed"
        with self.assertRaises(ValueError):
            MassOutageProvider.decode("53033", p)

    def test_wrong_county_rejected(self):
        with self.assertRaises(ValueError):
            MassOutageProvider.decode("53061", self.payload())

    def test_bad_count_rejected(self):
        for bad in ("12000", -1, 12.1, True, None):
            with self.assertRaises(ValueError):
                nonnegative_int(bad)

    def test_aware_timestamps_only(self):
        with self.assertRaises(ValueError):
            parse_time("2026-11-10T12:00:00")
        self.assertEqual(parse_time(iso(BASE)), BASE)

    def test_urls_require_https_and_no_markup_injection(self):
        for url in ("http://example.com", "https://example.com|@here", "https://a:b@example.com"):
            with self.assertRaises(ValueError):
                safe_url(url)

    def test_json_duplicates_rejected_not_double_counted(self):
        row = {"fips": "53033", "customers_out": 12000, "observed_at": iso(BASE),
               "source": "Fixture", "source_url": "https://example.com", "recovery_allowed": True}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "snapshots.json"
            path.write_text(json.dumps({"counties": [row, row]}))
            with self.assertRaises(ValueError):
                JsonProvider(Config(), str(path)).fetch()

    def test_slack_api_false_ok_is_failure(self):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "test", "SLACK_CHANNEL_ID": "C123"}, clear=True):
            sender = SlackSender()
        with patch("bot.http_request", return_value={"ok": False, "error": "not_in_channel"}):
            with self.assertRaises(RemoteError):
                sender.send("Test")

    def test_slack_thread_is_not_broadcast(self):
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "test", "SLACK_CHANNEL_ID": "C123"}, clear=True):
            sender = SlackSender()
        with patch("bot.http_request", return_value={"ok": True, "ts": "123.456"}) as call:
            sender.send("Test", "1.2")
            self.assertEqual(call.call_args.kwargs["payload"]["thread_ts"], "1.2")
            self.assertFalse(call.call_args.kwargs["payload"]["reply_broadcast"])

    def test_no_fake_webhook_host(self):
        with patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://example.com/services/fake"}, clear=True):
            with self.assertRaises(ValueError):
                SlackSender()



class AdditionalUtilityAdapterTests(unittest.TestCase):
    def test_arcgis_provider_sums_customer_fields(self):
        from bot import ArcGISProvider, Config
        payload = {
            "features": [
                {"attributes": {"customers_affected": 120, "last_updated": 1762786500000}},
                {"attributes": {"customers_affected": 80, "last_updated": 1762786500000}},
            ]
        }
        p = ArcGISProvider(Config(), "tacoma", "Tacoma Power", "TACOMA_OUTAGE_URL")
        rows, issues = p.parse_payload(payload)
        self.assertEqual(issues, [])
        self.assertEqual(rows["tacoma"].count, 200)


    def test_tacoma_uses_arcgis_default_and_filters_active_unplanned(self):
        from bot import ArcGISProvider, Config
        base = "https://services.arcgis.com/BLN4oKB0N1YSgvY8/ArcGIS/rest/services/Power_Outages_%28View%29/FeatureServer/0"
        payload = {"features": [
            {"attributes": {"UtilityCompany": "Tacoma Power", "OutageStatus": "Active", "OutageType": "Not Planned", "customersimpacted": 5}},
            {"attributes": {"UtilityCompany": "Tacoma Power", "OutageStatus": "Active", "OutageType": "Not Planned", "customersimpacted": 7}},
        ]}
        metadata = {"editingInfo": {"lastEditDate": int(time.time()*1000)}}
        with patch.dict("os.environ", {}, clear=True):
            p = ArcGISProvider(Config(), "tacoma", "Tacoma Power", "TACOMA_OUTAGE_URL")
            with patch("bot.http_request", side_effect=[payload, metadata]) as mocked:
                rows, issues = p.fetch()
        self.assertEqual(issues, [])
        self.assertEqual(rows["tacoma"].count, 12)
        self.assertEqual(mocked.call_count, 2)

    def test_json_utility_provider_parses_incident_list(self):
        from bot import JSONUtilityProvider, Config
        ts = "2026-11-10T22:55:00+00:00"
        payload = {"incidents": [
            {"customersImpacted": 50, "updated": ts},
            {"customersImpacted": 70, "updated": ts},
        ]}
        p = JSONUtilityProvider(Config(), "scl", "Seattle City Light", "SCL_OUTAGE_URL")
        rows, issues = p.parse_payload(payload)
        self.assertEqual(issues, [])
        self.assertEqual(rows["scl"].count, 120)


    def test_massoutage_utility_provider_aggregates_matching_counties(self):
        from bot import Config, MassOutageUtilityProvider
        import tempfile
        now = time.time()
        stamp = iso(now)
        payloads = {
            "53061": {"data": {"utilities": [{"name": "Snohomish County PUD", "customers_out": 1200}], "as_of": stamp}},
            "53029": {"data": {"utilities": [{"name": "Snohomish County PUD", "customers_out": 300}], "as_of": stamp}},
        }
        env = os.environ.copy()
        with tempfile.TemporaryDirectory() as td:
            os.environ["MASSOUTAGE_CACHE_DIR"] = td
            try:
                p = MassOutageUtilityProvider(Config(), "snopud", "Snohomish County PUD", ("53061", "53029"), ("snohomish county pud",))
                with patch("bot.http_request", side_effect=lambda url: payloads[url.rsplit("/", 1)[-1]]):
                    rows, issues = p.fetch()
            finally:
                os.environ.clear(); os.environ.update(env)
        self.assertEqual(issues, [])
        self.assertEqual(rows["snopud"].count, 1500)

    def test_massoutage_utility_provider_fails_closed_on_missing_utility(self):
        from bot import Config, MassOutageUtilityProvider
        p = MassOutageUtilityProvider(Config(), "scl", "Seattle City Light", ("53033",), ("seattle city light",))
        payload = {"data": {"utilities": [{"name": "Puget Sound Energy", "customers_out": 100}], "as_of": "2026-11-10T23:00:00Z"}}
        with patch("bot.http_request", return_value=payload):
            rows, issues = p.fetch()
        self.assertEqual(rows, {})
        self.assertTrue(issues)

    def test_unconfigured_secondary_sources_fail_closed(self):
        from bot import Config, UtilityAggregateProvider
        cfg = Config(utility_sources=("pse", "scl"))
        provider = UtilityAggregateProvider(cfg)
        rows, issues = provider.fetch()
        self.assertTrue(any("Seattle City Light" in x for x in issues))


class PowerOutageTrackerApiTests(unittest.TestCase):
    def test_scl_api_parser(self):
        stamp = iso(BASE)
        payload = {"state": {"last_updated": stamp, "utilities": [{"name": "Seattle City Light", "customers_out": 249, "last_updated": stamp}]}}
        snap = PowerOutageTrackerUtilityProvider.parse_payload(payload, "scl", "Seattle City Light", ("seattle city light",), BASE + 60)
        self.assertEqual(snap.count, 249)

    def test_snopud_api_parser(self):
        stamp = iso(BASE)
        payload = {"state": {"last_updated": stamp, "utilities": [{"name": "PUD No 1 of Snohomish County - (WA)", "customers_out": 8, "last_updated": stamp}]}}
        snap = PowerOutageTrackerUtilityProvider.parse_payload(payload, "snopud", "Snohomish County PUD", ("snohomish county pud", "pud no 1 of snohomish county"), BASE + 60)
        self.assertEqual(snap.count, 8)

    def test_api_parser_uses_root_freshness_when_utility_record_has_none(self):
        stamp = iso(BASE)
        payload = {"updated": stamp, "utilities": [{"name": "Seattle City Light", "customers_out": 12}]}
        snap = PowerOutageTrackerUtilityProvider.parse_payload(payload, "scl", "Seattle City Light", ("seattle city light",), BASE + 30)
        self.assertEqual(snap.count, 12)

    def test_api_parser_rejects_stale(self):
        stamp = iso(BASE)
        payload = {"updated": stamp, "utilities": [{"name": "Seattle City Light", "customers_out": 12}]}
        with self.assertRaises(ValueError):
            PowerOutageTrackerUtilityProvider.parse_payload(payload, "scl", "Seattle City Light", ("seattle city light",), BASE + 21*60)

if __name__ == "__main__":
    unittest.main()

class MassOutageCacheTests(unittest.TestCase):
    def test_keyless_provider_uses_five_minute_cache(self):
        import tempfile
        from unittest.mock import patch
        cfg = Config(source_kind="utility", utility_sources=("scl",))
        with tempfile.TemporaryDirectory() as td:
            env = os.environ.copy()
            os.environ["MASSOUTAGE_CACHE_DIR"] = td
            try:
                provider = MassOutageUtilityProvider(cfg, "scl", "Seattle City Light", ("53033",), ("seattle city light",))
                payload = {"data": {"fips": "53033", "as_of": "2026-11-10T23:14:00Z", "utilities": [{"name": "Seattle City Light", "customers_out": 129}]}}
                with patch("bot.http_request", return_value=payload) as mocked:
                    first = provider._fetch_county("53033")
                    second = provider._fetch_county("53033")
                self.assertEqual(first[0], 129)
                self.assertEqual(second[0], 129)
                self.assertEqual(mocked.call_count, 1)
            finally:
                os.environ.clear(); os.environ.update(env)

    def test_keyless_cache_is_separate_per_utility(self):
        import tempfile
        from unittest.mock import patch
        cfg = Config(source_kind="utility", utility_sources=("scl", "snopud"))
        with tempfile.TemporaryDirectory() as td:
            env = os.environ.copy(); os.environ["MASSOUTAGE_CACHE_DIR"] = td
            try:
                with patch("bot.http_request", return_value={"data": {"fips": "53033", "as_of": "2026-11-10T23:14:00Z", "utilities": [{"name": "Seattle City Light", "customers_out": 129}]}}):
                    a = MassOutageUtilityProvider(cfg, "scl", "Seattle City Light", ("53033",), ("seattle city light",))
                    a._fetch_county("53033")
                with patch("bot.http_request", return_value={"data": {"fips": "53061", "as_of": "2026-11-10T23:14:00Z", "utilities": [{"name": "PUD No 1 of Snohomish County - (WA)", "customers_out": 7}]}}):
                    b = MassOutageUtilityProvider(cfg, "snopud", "Snohomish County PUD", ("53061",), ("snohomish county",))
                    b._fetch_county("53061")
                self.assertTrue(os.path.exists(os.path.join(td, "scl.json")))
                self.assertTrue(os.path.exists(os.path.join(td, "snopud.json")))
            finally:
                os.environ.clear(); os.environ.update(env)

class SlackOneOffTestTests(unittest.TestCase):
    def test_slack_test_sends_one_explicit_message_without_monitoring(self):
        from bot import send_slack_test
        with patch("bot.SlackSender") as sender_type:
            sender_type.return_value.send.return_value = "1731770000.000100"
            self.assertEqual(send_slack_test(), 0)
            sender_type.assert_called_once_with()
            sender_type.return_value.send.assert_called_once()
            message = sender_type.return_value.send.call_args.args[0]
            self.assertIn("Slack delivery test", message)
            self.assertIn("not an outage alert", message)

class DiagnosticLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = Config(scope="regional", source_kind="massoutage",
                          counties={"53033": "King", "53061": "Snohomish"})
        self.sender = RecordingSender()
        self.store = StateStore(str(Path(self.temp.name) / "diagnostic.db"))
        self.engine = Engine(self.cfg, self.store, self.sender)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def sample(self, minute, king=6000, snohomish=3000, recovery=True):
        observed = BASE + minute * 60
        return {
            "53033": Snapshot("53033", "King County", king, observed,
                              "MassOutage", "https://massoutage.com/washington/king", recovery),
            "53061": Snapshot("53061", "Snohomish County", snohomish, observed,
                              "MassOutage", "https://massoutage.com/washington/snohomish", recovery),
        }

    def test_diagnostic_logs_combined_total_and_below_threshold_reason(self):
        samples = self.sample(0, king=6000, snohomish=3000)
        self.engine.observe(samples, BASE)
        with self.assertLogs("outage-bot", level="INFO") as captured:
            reason = self.engine.log_diagnostics(samples, BASE)
        joined = "\\n".join(captured.output)
        self.assertIn("coverage=2/2", joined)
        self.assertIn("total=9,000", joined)
        self.assertIn("above_threshold=no", joined)
        self.assertIn("not above 10,000", reason)

    def test_diagnostic_logs_duration_before_threshold_qualifies(self):
        first = self.sample(0, king=9000, snohomish=2000)
        self.engine.observe(first, BASE)
        later = self.sample(10, king=9000, snohomish=2000)
        self.engine.observe(later, BASE + 600)
        with self.assertLogs("outage-bot", level="INFO") as captured:
            reason = self.engine.log_diagnostics(later, BASE + 600)
        joined = "\\n".join(captured.output)
        self.assertIn("total=11,000", joined)
        self.assertIn("duration_above_threshold=600s", joined)
        self.assertIn("qualified=no", joined)
        self.assertIn("needs >900s", reason)

    def test_diagnostic_explains_incomplete_county_coverage(self):
        partial = self.sample(0)
        partial.pop("53061")
        self.engine.observe(partial, BASE)
        with self.assertLogs("outage-bot", level="WARNING") as captured:
            reason = self.engine.log_diagnostics(partial, BASE)
        self.assertIn("coverage=1/2", "\\n".join(captured.output))
        self.assertIn("total=UNKNOWN", "\\n".join(captured.output))
        self.assertIn("BLOCKED", "\\n".join(captured.output))
        self.assertIn("unavailable", reason)
