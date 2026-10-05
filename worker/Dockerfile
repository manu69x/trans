FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# gcc/libpq-dev for psycopg2/pgvector source builds (wheels preferred).
# tesseract-ocr is the local OCR engine of the 'ocr' queue (PRD §5.2 step 4,
# ADR-002 L3/L4): the OCR tasks need the binary at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
COPY worker/ ./worker/

RUN pip install --no-cache-dir -r requirements.txt

# Compose wires REDIS_URL / DATABASE_URL / MINIO_*.
CMD ["celery", "-A", "worker.celery:app", "worker", "--loglevel=info"]
