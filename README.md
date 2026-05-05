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
