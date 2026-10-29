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
# Lo stato reale è quello dei healthcheck Docker (docker compose ps):
# endpoint HTTP /health non esistono su db/redis/minio e il frontend è su 3002.
status: "docker compose -f infra/docker-compose.yml ps"

# --- Database ---------------------------------------------------------------

# Lo schema è gestito da Alembic (nessun init SQL): la migrazione gira nel
# container backend, dove vivono alembic.ini e migrations/.
migrate: "docker compose -f infra/docker-compose.yml exec backend alembic upgrade head"
db-shell: "docker compose -f infra/docker-compose.yml exec db psql -U trans -d trans"

# --- Maintenance ------------------------------------------------------------

clean: "docker compose -f infra/docker-compose.yml down -v --remove-orphans"
prune: "docker system prune -f && docker volume prune -f"

.DEFAULT_GOAL := up
