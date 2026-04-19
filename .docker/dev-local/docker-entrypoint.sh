#!/bin/bash
set -e

echo "==> Compiling SCSS (Step 6: make_style.sh)"
cd /code
sass resources:sass_processed --no-source-map --style=expanded --quiet
postcss \
    sass_processed/style.css \
    sass_processed/content-description.css \
    sass_processed/table.css \
    sass_processed/ranks.css \
    --use autoprefixer -d resources --no-map
echo "    SCSS done."

echo "==> Collecting static files"
python3 manage.py collectstatic --noinput -v 0

echo "==> Compiling translations"
python3 manage.py compilemessages -v 0 2>/dev/null || true
python3 manage.py compilejsi18n -v 0 2>/dev/null || true

echo "==> Running migrations"
python3 manage.py migrate --noinput -v 0

# Load initial fixtures on first run (detect via empty navbar table)
echo "==> Checking initial data..."
NAVBAR_COUNT=$(python3 manage.py shell -c "from judge.models import NavigationBar; print(NavigationBar.objects.count())" 2>/dev/null || echo "0")
if [ "$NAVBAR_COUNT" = "0" ]; then
    echo "    Loading initial fixtures (navbar, languages)..."
    python3 manage.py loaddata navbar -v 0
    python3 manage.py loaddata language_small -v 0
    echo "    Done. Run 'docker compose run --rm web python3 manage.py createsuperuser' to create an admin user."
fi

echo "==> Starting server..."
exec "$@"
