# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Abhaengigkeiten zuerst - die aendern sich seltener als der Code und
# bleiben dadurch im Build-Cache. requirements-web.txt zieht die Kernpakete
# ueber "-r requirements.txt" mit herein.
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

COPY xbot/ ./xbot/
COPY content/ ./content/
COPY config.example.yaml .env.example ./

# Nicht als root laufen lassen.
RUN useradd --create-home --shell /usr/sbin/nologin xbot \
    && mkdir -p /app/data /app/logs \
    && chown -R xbot:xbot /app
USER xbot

VOLUME ["/app/data", "/app/logs"]
EXPOSE 8080

# Standard ist der Probelauf. Fuer den Echtbetrieb XBOT_DRY_RUN=false setzen
# oder den Schalter in der Weboberflaeche umlegen.
ENV XBOT_DRY_RUN=true

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/login', timeout=4).status == 200 else 1)"

ENTRYPOINT ["python", "-m", "xbot"]
# Im Container muss auf 0.0.0.0 gelauscht werden, damit der Port nach
# aussen durchgereicht werden kann. Deshalb ist XBOT_WEB_PASSWORD Pflicht.
CMD ["web", "--host", "0.0.0.0", "--port", "8080"]
