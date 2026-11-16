FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 outage && useradd --uid 10001 --gid outage --no-create-home outage
WORKDIR /app
COPY --chown=10001:10001 bot.py /app/bot.py
RUN mkdir /app/data && chown 10001:10001 /app/data
USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "bot.py"]
