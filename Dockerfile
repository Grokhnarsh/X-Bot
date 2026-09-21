# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Abhaengigkeiten zuerst - die aendern sich seltener als der Code und
# bleiben dadurch im Build-Cache.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY xbot/ ./xbot/
COPY content/ ./content/
COPY config.example.yaml .env.example ./

# Nicht als root laufen lassen.
RUN useradd --create-home --shell /usr/sbin/nologin xbot \
    && mkdir -p /app/data /app/logs \
    && chown -R xbot:xbot /app
USER xbot

VOLUME ["/app/data", "/app/logs"]

# Standard ist der Probelauf. Fuer den Echtbetrieb XBOT_DRY_RUN=false setzen
# oder "run --live" als Kommando uebergeben.
ENV XBOT_DRY_RUN=true

ENTRYPOINT ["python", "-m", "xbot"]
CMD ["run"]
