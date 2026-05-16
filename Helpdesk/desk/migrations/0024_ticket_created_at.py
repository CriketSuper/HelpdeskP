from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("desk", "0023_ticket_participants_notification"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="created_at",
            field=models.DateTimeField(
                auto_now_add=True,
                db_index=True,
                default=django.utils.timezone.now,
                verbose_name="Дата и время создания",
            ),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="ticket",
            name="published",
            field=models.DateTimeField(
                auto_now=True,
                db_index=True,
                verbose_name="Дата и время изменения",
            ),
        ),
    ]
