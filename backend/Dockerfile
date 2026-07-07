# Argus backend — asynchronous trading engine + nightly optimizer.
FROM python:3.11-slim

# All internal daily resets and trading windows conform to Swiss time.
ENV TZ=Europe/Zurich \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/lib \
    DB_PATH=/app/shared/argus_state.db

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
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

RUN mkdir -p /app/shared

WORKDIR /app/backend

EXPOSE 8000

CMD ["python", "bot.py"]
