from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("desk", "0030_ticket_submission_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="attention_danger_notified_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="Время отправки критического предупреждения",
            ),
        ),
        migrations.AddField(
            model_name="ticket",
            name="attention_warning_notified_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="Время отправки предупреждения",
            ),
        ),
    ]
