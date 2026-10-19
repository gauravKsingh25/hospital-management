# Runbook — when something is wrong

Short, symptom-first fixes for the problems this system actually has. Each
entry: what you see → why → what to do. Background and rationale live in the
[README](../README.md); this page is for when you need the answer now.

---

## First, find the request

Every API response carries an `X-Request-ID` header, every backend log line
prints it in brackets, and a 500 response includes it in the body:

```json
{ "error": { "code": "internal_error", "message": "…", "request_id": "3f9c…" } }
```

Find it in the browser's Network tab (the `/bff/...` request → Response
headers), then search the API's log output for that value. You get every line
that request wrote, including the ones from event handlers it triggered.

```
11:40:02 ERROR    [3f9c1e…] app.core.exceptions : unhandled exception: …
```

A load balancer or another service can pass its own `X-Request-ID`; if it is
safe (letters, digits, `.`, `_`, `-`, up to 64 characters) it is kept, so one ID
can follow a request across systems.

---

## "The app opens but sign-in fails" / "everything is empty"

**Why:** Postgres is down. `/health` still says `ok` because it deliberately
touches nothing — it only answers "is the process up?".

**Do:**

```bash
docker ps                                 # are postgres and redis listed and healthy?
docker compose up -d postgres redis       # start them if not
curl http://localhost:8000/health/ready   # checks database AND redis
```

Docker Desktop restarts stop these containers; this is the most common cause.

## "Every request takes ~4 seconds" / "the tests seem to hang"

**Why:** Redis is down. The RBAC permission cache reads it on every
authenticated request and waits out a connect retry before falling back to the
database. Correct, but slow.

**Do:** `docker compose up -d redis`, then confirm with `/health/redis`.

## "The first request after a while is very slow"

**Why:** Neon's free tier suspends idle compute; the next connection pays a cold
start. Expected, not an error.

**Do:** nothing. If *every* request is slow, read
[Database latency](../README.md#database-latency-read-this-before-blaming-the-ui)
— a distant Neon region costs ~870ms per SQL round trip from India.

## "The API logs `rls_enforced=False`" / "refuses to boot in production"

**Why:** `DATABASE_URL` connects as a role that can bypass row-level security
(usually the database owner). Tenant isolation is then not enforced at all.

**Do:**

```bash
cd backend
python scripts/bootstrap_db_role.py   # creates the restricted hms_app role
```

Then point `DATABASE_URL` at `hms_app` and restart. Keep `DIRECT_URL` on the
owner — migrations need it.

## "My backend change has no effect" (Windows)

**Why:** `uvicorn --reload` started from a background shell on Windows can log
"Reloading…" and keep serving the old code.

**Do:** stop the server and start it again without `--reload`:

```bash
cd backend
PYTHONUTF8=1 ../.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`PYTHONUTF8=1` matters too: without it, logging Hindi text or `₹` raises
`UnicodeEncodeError`. Confirm a new route exists via `/openapi.json`.

## "The Dockerised api cannot reach the database"

**Why:** `.env` holds host-machine values. `localhost:5432` inside the `api`
container is the container itself.

**Do:** either run the API on the host (above), or point both URLs at the
compose service name, as shown at the top of `docker-compose.yml`:
`postgres:5432`.

## "A migration produced an empty file" / "autogenerate wants to drop tables"

**Why:** the module's `models.py` is not imported by `backend/app/registry.py`,
so Alembic cannot see its tables.

**Do:** add the import there, delete the bad revision, and autogenerate again.
Never edit the database by hand.

```bash
cd backend
alembic current            # where the database is
alembic upgrade head       # bring it up to date
alembic downgrade -1       # undo the last revision
```

## "E2E specs fail with timeouts on the billing / lab boards"

**Why:** accumulated test visits. Boards are ordered oldest-first, so after
many runs a fresh visit lands on the last page.

**Do:**

```bash
cd backend
python scripts/reset_demo_data.py --yes   # local databases only; keeps staff, prices, wards
python scripts/seed_demo.py
```

## "The frontend build fails with `Generated API types are out of date`"

**Why:** a backend schema or permission changed and the committed TypeScript
types were not regenerated.

**Do:**

```bash
cd frontend
npm run codegen         # regenerate openapi.json, openapi.d.ts, rbac.generated.ts
npm run codegen:check   # what CI runs
```

---

## Health endpoints

| Endpoint | Answers | Touches |
|---|---|---|
| `/health` | is the process up? | nothing |
| `/health/db` | can it reach Postgres, how fast, is RLS enforced? | database |
| `/health/redis` | can it reach Redis? | redis |
| `/health/ready` | can this instance serve traffic? | both — 503 if either fails |

## Before every commit

```bash
cd backend  && ruff check . && ruff format --check . && mypy app && pytest
cd frontend && npm run verify     # codegen check + lint + typecheck + build
```
