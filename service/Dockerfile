# Cloud Run container for Extract Data HTTP API
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Install system deps if needed (kept minimal)
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

# Gunicorn binds to $PORT for Cloud Run
CMD exec gunicorn --workers=1 --threads=8 --bind 0.0.0.0:${PORT} main:app

