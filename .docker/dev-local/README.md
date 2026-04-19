# Docker Dev Guide

Runs an Ubuntu 22.04 environment matching the production server.
All code is live-mounted so edits on your host appear instantly inside the container.

All Docker config lives here: `online-judge/.docker/dev-local/`

---

## Platform support

| Platform | Status | Notes |
|---|---|---|
| **Linux (Ubuntu 22.04+)** | ✅ Full support | Install Docker Engine, no extra steps |
| **macOS (Intel + Apple Silicon)** | ✅ Full support | Install Docker Desktop; ARM64 images used natively on M-series |
| **Windows 10/11** | ✅ Full support | Requires Docker Desktop + **WSL 2 backend** (see below) |

### Windows prerequisites

1. Enable WSL 2: `wsl --install` in PowerShell (admin), then reboot
2. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) — enable the WSL 2 backend in Settings → General
3. Clone the repo **inside WSL** (not on the Windows NTFS drive) to avoid volume-mount slowness:
   ```bash
   # Inside WSL terminal
   cd ~
   git clone <repo-url> LQDOJ
   ```
4. Run all commands from the WSL terminal, not PowerShell

> **Line endings** — `.gitattributes` at the repo root forces LF on all shell scripts.
> If you cloned before it existed, run: `git rm --cached -r . && git reset --hard`

---

## How to run

All commands below use `-f` so you can run them from **the repo root** (`LQDOJ/`).
If you prefer, `cd online-judge/.docker/dev-local` and drop the `-f` flag entirely.

```bash
# Shorthand — set once per terminal session to avoid typing -f every time
export COMPOSE_FILE=online-judge/.docker/dev-local/docker-compose.yml
```

---

## First-time setup

```bash
cd ~/hobby-projects/LQDOJ

# Build the image (once, or after Dockerfile / requirements.txt changes)
docker compose -f online-judge/.docker/dev-local/docker-compose.yml build

# Start everything
docker compose -f online-judge/.docker/dev-local/docker-compose.yml up
```

On first start the entrypoint automatically:
1. Compiles SCSS → CSS
2. Collects static files
3. Runs `migrate`
4. Loads `navbar` + `language_small` fixtures (only if DB is empty)
5. Starts `runserver` on **http://localhost:8001**

Then in a separate terminal:
```bash
# Create your admin account (once)
docker compose -f online-judge/.docker/dev-local/docker-compose.yml \
    run --rm web python3 manage.py createsuperuser

# Seed the DB with 60 users / 210 problems / 27 contests
docker compose -f online-judge/.docker/dev-local/docker-compose.yml \
    run --rm web python3 manage.py seed_dev_data
```

---

## Daily workflow

```bash
COMPOSE_FILE=online-judge/.docker/dev-local/docker-compose.yml

docker compose -f $COMPOSE_FILE up        # start; Ctrl-C to stop
docker compose -f $COMPOSE_FILE down      # stop (DB data kept)
docker compose -f $COMPOSE_FILE down -v   # stop + wipe DB completely
```

---

## Rebuild reference

### After Python / template changes
No action needed. Django `runserver` auto-reloads on every file save.

---

### After adding or changing a model (migrations)

```bash
docker compose -f online-judge/.docker/dev-local/docker-compose.yml \
    run --rm web python3 manage.py makemigrations
docker compose -f online-judge/.docker/dev-local/docker-compose.yml \
    run --rm web python3 manage.py migrate
```

---

### After editing SCSS (`.scss` files)

```bash
docker compose -f online-judge/.docker/dev-local/docker-compose.yml \
    run --rm --entrypoint bash web -c "
  sass resources:sass_processed --no-source-map --style=expanded --quiet
  postcss sass_processed/style.css sass_processed/content-description.css \
          sass_processed/table.css sass_processed/ranks.css \
          --use autoprefixer -d resources --no-map
"
```

CSS lands in `online-judge/resources/` (live-mounted). Hard-refresh the browser.

---

### After editing static files (JS, images, compiled CSS)

```bash
docker compose -f online-judge/.docker/dev-local/docker-compose.yml \
    exec web python3 manage.py collectstatic --noinput -v 0
```

Hard-refresh (Cmd/Ctrl + Shift + R) after running.

> Must be run after every SCSS recompile AND after any direct edit to JS or images.

---

### After editing `requirements.txt`

```bash
docker compose -f online-judge/.docker/dev-local/docker-compose.yml build
docker compose -f online-judge/.docker/dev-local/docker-compose.yml up
```

---

### After editing `Dockerfile` or `docker-entrypoint.sh`

```bash
docker compose -f online-judge/.docker/dev-local/docker-compose.yml build
docker compose -f online-judge/.docker/dev-local/docker-compose.yml up
```

---

### After editing `local_settings_docker.py`

```bash
docker compose -f online-judge/.docker/dev-local/docker-compose.yml restart web
```

Settings are read at startup — restart is enough, no rebuild needed.

---

### After pulling new code with DB migrations

```bash
docker compose -f online-judge/.docker/dev-local/docker-compose.yml \
    run --rm web python3 manage.py migrate
```

---

## Useful one-liners

```bash
CF="-f online-judge/.docker/dev-local/docker-compose.yml"

# Open a shell inside the running container
docker compose $CF exec web bash

# Run any management command
docker compose $CF run --rm web python3 manage.py <command>

# Tail Django logs
docker compose $CF logs -f web

# Re-seed dev data from scratch
docker compose $CF run --rm web python3 manage.py seed_dev_data --clear

# Django shell
docker compose $CF run --rm web python3 manage.py shell

# Check for config errors
docker compose $CF run --rm web python3 manage.py check
```

---

## File map

```
online-judge/.docker/dev-local/
├── docker-compose.yml       — service definitions
├── Dockerfile               — Ubuntu 22.04 image + Python + Node
├── docker-entrypoint.sh     — startup script (SCSS, migrate, fixtures)
├── .dockerignore            — excludes from build context
├── local_settings_docker.py — Django settings (mounted as local_settings.py)
├── mariadb-init/
│   └── 01-timezone.sh       — loads tz data into MariaDB on first start
└── README.md                — this file
```

---

## Services and ports

| Service    | Host port | What it is               |
|------------|-----------|--------------------------|
| `web`      | **8001**  | Django runserver         |
| `db`       | —         | MariaDB 10.11 (internal) |
| `memcached`| —         | Cache (internal)         |
| `redis`    | —         | Celery broker (internal) |

---

## Troubleshooting

**"Table doesn't exist" after pulling new code**
```bash
docker compose -f online-judge/.docker/dev-local/docker-compose.yml \
    run --rm web python3 manage.py migrate
```

**Port 8001 already in use**
```bash
lsof -i :8001                          # macOS / Linux
netstat -ano | findstr 8001            # Windows PowerShell
```

**CSS looks wrong after an SCSS change**
Re-run the SCSS compile command above, then hard-refresh.

**Windows: `/bin/bash^M: bad interpreter`**
Shell scripts have Windows line endings. Fix:
```bash
# Inside WSL
dos2unix online-judge/.docker/dev-local/docker-entrypoint.sh
dos2unix online-judge/.docker/dev-local/mariadb-init/01-timezone.sh
docker compose -f online-judge/.docker/dev-local/docker-compose.yml build
```

**Full reset**
```bash
docker compose -f online-judge/.docker/dev-local/docker-compose.yml down -v
docker compose -f online-judge/.docker/dev-local/docker-compose.yml build
docker compose -f online-judge/.docker/dev-local/docker-compose.yml up
```
