# Argus — single-container image: equities engine, crypto engine, and the
# NiceGUI dashboard, run as three supervisord-managed processes so a crash
# in one doesn't take the others down (see deploy/supervisord.conf).
FROM python:3.11-slim

# All internal daily resets, trading windows and rendered timestamps
# conform to Swiss time.
ENV TZ=Europe/Zurich \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/lib \
    DB_PATH=/app/shared/argus_state.db

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata supervisor \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# The shared package is installed to /app/lib (on PYTHONPATH) so the
# /app/shared mount point stays a pure data directory for the SQLite volume.
COPY shared/ /app/lib/shared/
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/
COPY deploy/supervisord.conf /etc/supervisor/conf.d/argus.conf

RUN mkdir -p /app/shared

EXPOSE 8000 8001 8080

CMD ["supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
