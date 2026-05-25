#!/bin/sh
set -e

mkdir -p /app/Helpdesk/staticfiles /app/Helpdesk/media /app/Helpdesk/logs

python manage.py migrate --noinput
python manage.py collectstatic --noinput

exec "$@"
