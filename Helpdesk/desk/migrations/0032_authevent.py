from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("desk", "0031_ticket_attention_notifications"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AuthEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("username", models.CharField(blank=True, max_length=255, verbose_name="Логин/ФИО")),
                (
                    "event_type",
                    models.CharField(
                        choices=[
                            ("login_success", "Успешный вход"),
                            ("login_failure", "Неуспешный вход"),
                            ("logout", "Выход"),
                        ],
                        max_length=32,
                        verbose_name="Событие",
                    ),
                ),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True, verbose_name="IP-адрес")),
                ("user_agent", models.CharField(blank=True, max_length=512, verbose_name="User-Agent")),
                ("metadata", models.JSONField(blank=True, default=dict, verbose_name="Метаданные")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Создано")),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.SET_NULL,
                        related_name="auth_events",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Пользователь",
                    ),
                ),
            ],
            options={
                "verbose_name": "Событие авторизации",
                "verbose_name_plural": "События авторизации",
                "ordering": ["-created_at"],
            },
        ),
    ]
