#!/bin/bash
# Run numbered SQL files from judge/sql/ against MariaDB.
# Usage:
#   ./judge/ml/setup.sh                # run all .sql files in order
#   ./judge/ml/setup.sh 001            # run only files matching prefix "001"
#
# DB connection is read from Django settings via a small Python helper,
# or can be overridden with environment variables:
#   DB_HOST, DB_NAME, DB_USER, DB_PASSWORD

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SQL_DIR="$(cd "$SCRIPT_DIR/../sql" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Read DB config from Django settings if env vars not set
if [ -z "${DB_HOST:-}" ] || [ -z "${DB_NAME:-}" ] || [ -z "${DB_USER:-}" ]; then
    eval "$(DJANGO_SETTINGS_MODULE=dmoj.settings PYTHONPATH="$PROJECT_DIR" python3 -c "
import django, os, shlex
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dmoj.settings')
django.setup()
from django.conf import settings
db = settings.DATABASES['default']
print(f'DB_HOST={shlex.quote(db[\"HOST\"])}')
print(f'DB_NAME={shlex.quote(db[\"NAME\"])}')
print(f'DB_USER={shlex.quote(db[\"USER\"])}')
print(f'DB_PASSWORD={shlex.quote(db.get(\"PASSWORD\", \"\"))}')
")"
fi

MYSQL_ARGS=(-h "$DB_HOST" -u "$DB_USER" "$DB_NAME")
if [ -n "${DB_PASSWORD:-}" ]; then
    MYSQL_ARGS+=(-p"$DB_PASSWORD")
fi

PREFIX="${1:-}"

for sql_file in "$SQL_DIR"/*.sql; do
    filename="$(basename "$sql_file")"
    if [ -n "$PREFIX" ] && [[ "$filename" != "$PREFIX"* ]]; then
        continue
    fi
    echo "==> Running $filename ..."
    mariadb "${MYSQL_ARGS[@]}" < "$sql_file"
    echo "    Done."
done

echo "All SQL files executed successfully."
