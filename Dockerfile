# syntax=docker/dockerfile:1

# --------------------------------------------------------------------------- #
# 4K Discovery — single self-contained image.
# Runs the FastAPI web server AND the in-process APScheduler scraper worker,
# with the SQLite database stored on a mounted volume at /app/data.
# --------------------------------------------------------------------------- #
FROM python:3.12-slim

# Keep Python lean & log-friendly inside containers.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data \
    DB_PATH=/app/data/deals.db \
    PORT=8000

WORKDIR /app

# System deps: curl is used by the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the application code.
COPY app ./app

# Persist the SQLite database outside the image layers.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Run as a non-root user for safety.
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# A single process serves the web app and runs the scheduler thread.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
