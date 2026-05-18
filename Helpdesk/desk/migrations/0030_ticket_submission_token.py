from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("desk", "0029_ticket_additional_executors"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="submission_token",
            field=models.CharField(
                blank=True,
                null=True,
                max_length=32,
                unique=True,
                db_index=True,
                editable=False,
                verbose_name="Токен создания",
            ),
        ),
    ]
