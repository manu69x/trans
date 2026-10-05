# Deployment & containers

How the Docker implementation works: the stack, how code changes reach the
containers, networking, and the most common operational issues.

---

## 1. Stack overview

All services run in containers managed by Docker Compose
(`infra/docker-compose.yml`):

| Service  | Container      | Image                     | Host port        | Notes                                                     |
|----------|----------------|---------------------------|------------------|-----------------------------------------------------------|
| DB       | `trans-db`     | `pgvector/pgvector:pg16`  | 5432             | volume `db-data`; schema via **Alembic** (no init SQL)    |
| Redis    | `trans-redis`  | `redis:7-alpine`          | 6379             | volume `redis-data`                                       |
| MinIO    | `trans-minio`  | see compose               | 9000 API / 9001 console | manuscript object storage                          |
| Backend  | `trans-backend`| build of `backend/Dockerfile` | 8000         | FastAPI; code is **installed in the image** (`pip install .`) |
| Worker   | `trans-worker` | build of `worker/Dockerfile` | —             | Celery; the operational path is in-process (ADR-007)      |
| Frontend | `trans-frontend`| build of `frontend/Dockerfile` | **3002** → 3000 | Next.js production build                              |

The frontend publish is 3002 → container 3000 so the stack can coexist with
another service on host port 3000; change the mapping in the compose file if
you prefer a different host port.

### Frontend Dockerfile (Next.js)

- `node:20-alpine`, `NODE_ENV=production`.
- Build stage: `COPY . .` + `npm run build` — the bundle is **baked into the
  image**, not served from the host filesystem.
- Start: `npm run start` (next start).
- Consequence: **frontend code changes require an image rebuild** — a
  container restart or page reload is not enough.

### Backend / worker Dockerfiles (Python)

- **Backend: the code is installed in the image** (`pip install .` from
  `pyproject.toml`; `alembic.ini` and `migrations/` are copied to `/app` so
  `alembic upgrade head` runs at boot). There is deliberately **no bind
  mount of the source** — a read-only `../backend:/app` mount hides the
  installed package and is misleading: **every backend change = image
  rebuild** (§2).
- Python dependencies are installed at build time; the backend reads them
  from `pyproject.toml` (not `requirements.txt`) — new dependencies go into
  pyproject and require a rebuild.
- Worker: mounts `../worker:/app/worker:ro` — the package, **not** `/app`:
  mounting the whole `/app` hides the package already in the image and
  Celery crash-loops with `No module named 'worker'`.

### Internal networking

- Containers reach each other by compose service name: `http://backend:8000`,
  `db:5432`, `minio:9000`, `redis:6379`.
- The frontend talks to the backend through a **server-side rewrite**
  (`BACKEND_URL=http://backend:8000` in the compose, consumed by
  `next.config.js`): the browser uses relative paths only (no CORS).
- The backend reaches the **LLM Gateway** (the only LLM provider, ADR-001)
  via `LLM_GATEWAY_BASE_URL` — by default
  `http://host.docker.internal:8080/v1`, since `host.docker.internal` is how
  containers reach a gateway running on the host, outside the compose
  network.

### Healthchecks & orchestration

- Every service has a healthcheck (db: `pg_isready`; backend: `GET /health`;
  frontend: internal `GET /health`; redis: `redis-cli ping`).
- `depends_on` with `condition: service_healthy`: the backend waits for
  db+redis+minio; the frontend waits for the backend.

---

## 2. Code changes → how they reach the containers

| Changed                    | What to do                                                                       |
|----------------------------|----------------------------------------------------------------------------------|
| `backend/**`               | **rebuild**: `docker compose -f infra/docker-compose.yml build backend && up -d backend` |
| `worker/**`                | rebuild worker (the Celery path is not operational, ADR-007)                      |
| `frontend/**`              | `docker compose -f infra/docker-compose.yml up -d --build frontend` — the bundle lives in the image |
| `infra/docker-compose.yml` | `up -d` to apply config                                                          |
| `backend/migrations/**`    | nothing: the backend runs `alembic upgrade head` at boot; to apply now: `just migrate` |
| New backend dependency     | `pyproject.toml` + backend rebuild                                                |
| New frontend dependency    | `package.json` + frontend rebuild                                                 |

### Verifying the frontend serves the new build

Next.js pages are **client-rendered**: the HTML only shows a loading
skeleton, so you cannot verify from the HTML source. Check the JS bundle:

```bash
curl -s http://localhost:3002/entita | grep -oE '/_next/static/chunks/app/entita/[^"]*\.js'
# fetch that chunk and look for the expected label/logic inside it
```

If you see old UI after a change, the almost-always cause is: the container
is still serving the previous build → rebuild (§2).

---

## 3. Long browser calls (bulk LLM actions)

The **Next.js rewrite proxy** (`/api/v1/*` in `next.config.js`) **truncates
responses at ~30 s** with "socket hang up" / ECONNRESET (visible in the
frontend container logs as "Failed to proxy"). Any action that can take
longer — bulk LLM translation, bulk status changes over thousands of rows —
must call the backend **directly**:

- Frontend `src/lib/api.ts`: `longApi()` →
  `http://<current host>:8000/api/v1` (host taken from the address bar, so
  it also works from the LAN). Used by the long-running bulk endpoints;
  SSR-safe (falls back to the proxy when `window` is undefined).
- Backend `backend/main.py`: CORS `allow_origin_regex` for local hosts
  (localhost, 127.0.0.1, `192.168.*`, `10.*`, `172.16-31.*`) on any port —
  with an allow-list limited to localhost, preflights from LAN origins
  return **400** and bulk actions stall on the first chunk.

Design rules learned the hard way (same symptom, three different causes):

1. Thousands of parallel PATCHes → "server closed the connection" + timeouts
   → bulk endpoints must run in a single transaction, in chunks.
2. 100-entity chunks take ≈ 50 s → truncated by the Next proxy at 30 s →
   chunk sizes must stay under the proxy timeout, or bypass it entirely.
3. CORS preflight 400 from LAN origins → permissive local-origin regex.

---

## 4. Recovery recipes

- **Zombie `running` jobs** (e.g. after a backend kill): cancel them
  explicitly so the UI stops polling:

  ```sql
  UPDATE jobs SET status='cancelled', completed_at=now()
  WHERE status IN ('running','queued') AND started_at IS NULL;
  ```

- **Backend restart-loop on a missing dependency**: exec into the container
  (`docker compose exec backend pip install --no-cache-dir <pkg>`) to
  confirm, then add the package to `pyproject.toml` and rebuild — the
  container filesystem is ephemeral.
- **Fresh volume, empty data**: data lives in the named volumes
  (`db-data`, `redis-data`, `minio-data`); `just clean` removes them
  (destructive). Backup/restore tooling lives in the backend
  (`backup.py`) and is exercised in the test suite.
- **Schema drift**: never hand-edit the DB; `just migrate` applies
  `backend/migrations/` — Alembic is the single source of truth.

## 5. Production profile

`infra/docker-compose.prod.yml` layers hardening on the base file:

- non-root users, `no-new-privileges`, resource limits, read-only rootfs
  where possible, log rotation;
- db/redis/minio **not published** on the host (internal network only);
- a single TLS entrypoint (nginx, self-signed LAN cert, `infra/nginx-tls.conf`)
  in front of backend + frontend;
- mandatory secrets from the environment / `infra/secrets/` (never
  committed): `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `MINIO_ACCESS_KEY` /
  `MINIO_SECRET_KEY`, `JWT_SECRET`, plus storage-at-rest and backup keys;

```bash
docker compose -f infra/docker-compose.yml \
               -f infra/docker-compose.prod.yml up -d
```
