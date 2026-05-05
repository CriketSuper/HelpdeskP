Helpdesk system, developed for diplom project.

The project is updated to Django 6 and reads configuration from the root
`.env` file.

Environment variables:

- `SECRET_KEY`
- `DEBUG`
- `ALLOWED_HOSTS`
- `DATABASE_URL`
- `EMAIL_HOST`
- `EMAIL_PORT`
- `EMAIL_USE_SSL`
- `EMAIL_HOST_USER`
- `EMAIL_HOST_PASSWORD`
- `DEFAULT_ADMIN_USERNAME`
- `DEFAULT_ADMIN_PASSWORD`
- `DEFAULT_ADMIN_EMAIL`
- `DEFAULT_ADMIN_VERBOSE_NAME`

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
