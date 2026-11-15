FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/app/data/lab.db \
    SEED_DATABASE_PATH=/app/seed/lab_wide.db \
    MIGRATION_CHUNK_SIZE=100 \
    MIGRATION_CHUNK_DELAY_MS=100

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY legacy ./legacy
COPY seed ./seed
COPY scripts ./scripts

RUN mkdir -p /app/data \
    && useradd --create-home --uid 10001 lab \
    && chown -R lab:lab /app/data

USER lab

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
