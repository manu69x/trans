# Trans — local literary translation platform (EN->IT).
# Local-only stack: postgres+pgvector, redis, minio, backend, frontend, worker.
# See README.md for the full port table.

# --- Lifecycle --------------------------------------------------------------

up: "docker compose -f infra/docker-compose.yml up -d"
down: "docker compose -f infra/docker-compose.yml down"
restart: "docker compose -f infra/docker-compose.yml restart"

# --- Observability ----------------------------------------------------------

logs: "docker compose -f infra/docker-compose.yml logs -f"
ps: "docker compose -f infra/docker-compose.yml ps"
# The real status is the Docker healthcheck state (docker compose ps):
# db/redis/minio expose no HTTP /health and the frontend is on host port 3002.
status: "docker compose -f infra/docker-compose.yml ps"

# --- Database ---------------------------------------------------------------

# The schema is managed by Alembic (no init SQL): migrations run inside the
# backend container, where alembic.ini and migrations/ live.
migrate: "docker compose -f infra/docker-compose.yml exec backend alembic upgrade head"
db-shell: "docker compose -f infra/docker-compose.yml exec db psql -U trans -d trans"

# --- Maintenance ------------------------------------------------------------

clean: "docker compose -f infra/docker-compose.yml down -v --remove-orphans"
prune: "docker system prune -f && docker volume prune -f"

.DEFAULT_GOAL := up
