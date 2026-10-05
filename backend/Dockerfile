FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# gcc/libpq-dev needed for psycopg2 and pgvector source builds (wheels preferred)
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml alembic.ini ./
COPY backend/ ./backend/
# Alembic migrations: the container entrypoint runs `alembic upgrade head`
# before uvicorn, so a fresh DB volume is migrated from zero (DR path).
COPY migrations/ ./migrations/

# Install the package (FastAPI, SQLAlchemy, psycopg2-binary, pgvector, ...).
RUN pip install --no-cache-dir .

# Healthcheck: DB must be reachable on :5432 before the API is trusted healthy.
HEALTHCHECK --interval=15s --timeout=5s --retries=5 --start-period=20s \
    CMD python -c "import socket;socket.create_connection(('db',5432),2).close()"

# Compose wires DATABASE_URL / REDIS_URL / MINIO_* into the environment.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
