FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY . .

WORKDIR /app/Helpdesk

RUN python manage.py migrate

EXPOSE 1234

CMD ["python", "manage.py", "runserver", "0.0.0.0:1234"]
