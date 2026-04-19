# LQDOJ Handoff

## Setup Status: Complete

Site runs at `http://localhost:8001`.

### Option A — Docker (recommended, zero lib-mismatch pain) — see `online-judge/.docker/dev-local/README.md`

Runs a real Linux environment (Python 3.11 / Debian Bookworm / MariaDB 10.11) identical to the server. Code is live-mounted so edits on Mac appear instantly.

**First-time setup:**
```bash
cd ~/hobby-projects/LQDOJ

# Start DB + services, wait for healthy
docker compose up -d db memcached redis

# Migrate database (once)
docker compose run --rm web python3 manage.py migrate

# Create admin user (once)
docker compose run --rm web python3 manage.py createsuperuser

# Collect static files (once, and after any static changes)
docker compose run --rm web python3 manage.py collectstatic --noinput

# Start the dev server
docker compose up web
```

**Daily usage:**
```bash
cd ~/hobby-projects/LQDOJ
docker compose -f online-judge/.docker/dev-local/docker-compose.yml up
```

**One-off management commands:**
```bash
docker compose -f online-judge/.docker/dev-local/docker-compose.yml run --rm web python3 manage.py <command>
```

**Tear down completely** (keeps DB data):
```bash
docker compose -f online-judge/.docker/dev-local/docker-compose.yml down
```
**Nuke DB data too:**
```bash
docker compose -f online-judge/.docker/dev-local/docker-compose.yml down -v
```

---

### Option B — Native Mac (legacy, avoid for new work)

- DB: MariaDB via Homebrew, user `dmoj`, password `dmoj`, database `dmoj`
- Venv: `~/hobby-projects/LQDOJ/dmojsite` (Python 3.14 — causes occasional Django deserialization bugs)
- `demo` fixture skipped — Python 3.14 / Django 4.2 deserialization bug

```bash
source ~/hobby-projects/LQDOJ/dmojsite/bin/activate
cd ~/hobby-projects/LQDOJ/online-judge
python3 manage.py runserver 0.0.0.0:8001
```

---

## Active Work: Code Quality PRs

Three branches created off `master`:

| Branch | PR Title | Status |
|--------|----------|--------|
| `chore/extract-constants` | Extract magic numbers into named constants | **ABANDONED** — most constants already exist (submission statuses on `Submission` model, priorities named in `judgeapi.py`). Not enough real duplication to justify. Delete this branch. |
| `chore/structured-logging` | Replace scattered logging with structured JSON logging | Todo |
| `chore/add-mypy` | Add mypy type checking to pre-commit pipeline | Todo |

---

## Architecture Notes

See conversation history for full architecture analysis. Key points:
- Judging pipeline: Browser → Django → Bridge (TCP/zlib) → Judge Server → event daemon → Browser
- 3-layer cache: L0 (request-scoped thread-local), L1 (memcached), L2 (`@cache_wrapper`)
- Celery + Redis for background tasks
- Jinja2 templates + jQuery + Socket.IO frontend
- `libseccomp` is Linux-only; judge sandbox won't run on macOS
