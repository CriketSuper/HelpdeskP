Helpdesk system, developed for diplom project.

The project is updated to Django 6 and reads configuration from the root
`.env` file.

Environment variables:

- `SECRET_KEY`
- `DEBUG`
- `ALLOWED_HOSTS`
- `DATABASE_URL`
- `MAX_UPLOAD_SIZE_MB`
- `EMAIL_HOST`
- `EMAIL_PORT`
- `EMAIL_USE_SSL`
- `EMAIL_HOST_USER`
- `EMAIL_HOST_PASSWORD`
- `DEFAULT_ADMIN_USERNAME`
- `DEFAULT_ADMIN_PASSWORD`
- `DEFAULT_ADMIN_EMAIL`
- `DEFAULT_ADMIN_VERBOSE_NAME`
- `CACHE_BACKEND`
- `CACHE_LOCATION`
- `LOGIN_RATE_LIMIT_ENABLED`
- `LOGIN_RATE_LIMIT_ATTEMPTS`
- `LOGIN_RATE_LIMIT_WINDOW_SECONDS`
- `LOGIN_RATE_LIMIT_BLOCK_SECONDS`
- `SYSTEM_STATUS_BACKUP_ROOT`

Local setup:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python Helpdesk\manage.py migrate
python Helpdesk\manage.py runserver
```

The first local admin user is created automatically on a new database.

Docker setup:

```powershell
docker compose up --build
```

`docker-compose.yml` uses the root `.env` file for runtime variables, but the
file is not copied into the image. Configuration is passed into containers via
Compose environment injection.

The compose stack starts three containers:

- `db` on `postgres:alpine`
- `web` with Django + `gunicorn`
- `nginx` for proxying the application and serving static/media files

Relevant environment variables for Docker:

- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `SECRET_KEY`
- `DEBUG`
- `ALLOWED_HOSTS`

Application startup inside the `web` container runs:

- `python manage.py migrate --noinput`
- `python manage.py collectstatic --noinput`
- `gunicorn helpdesk.wsgi:application --bind 0.0.0.0:8000`

Backups:

- Copy `backup.env.example` to `backup.env`
- Set `BACKUP_MODE=docker` for the current compose stack, or `host` if you run PostgreSQL and media outside Docker
- Adjust `BACKUP_ROOT`, PostgreSQL credentials, and media paths

Available scripts:

- `scripts/backup.sh` creates a timestamped PostgreSQL dump, media archive, and logs archive
- `scripts/cleanup_backups.sh` removes backup directories older than `RETENTION_DAYS`
- `scripts/restore_db.sh <backup_dir_or_db.dump>` restores the database
- `scripts/restore_media.sh <backup_dir_or_media.tar.gz>` restores media files
- `scripts/restore_logs.sh <backup_dir_or_logs.tar.gz>` restores application logs

Example cron:

```cron
0 2 * * * /srv/Helpdesk/scripts/backup.sh >> /var/log/helpdesk-backup.log 2>&1
30 2 * * * /srv/Helpdesk/scripts/cleanup_backups.sh >> /var/log/helpdesk-backup.log 2>&1
```

Role matrix:

- `Админ`:
  full access to all tickets, statistics, system status, and user administration through Django admin
- `Директор`:
  can participate in workflow, can be assigned as executor, can view statistics only for accessible tickets
- `Исполнитель`:
  can work with assigned and co-assigned tickets, can view statistics only for accessible tickets
- user without groups:
  can only view own tickets and own ticket history

Operational and security features:

- `GET /desk/health/` returns a health snapshot for app checks
- `/desk/system-status/` shows database, mail, media, logs, backup status, and recent auth events for admins
- login rate limiting uses Django cache and blocks repeated failed attempts
- successful logins, failed logins, and logouts are stored in the `AuthEvent` journal and visible in admin/system status
