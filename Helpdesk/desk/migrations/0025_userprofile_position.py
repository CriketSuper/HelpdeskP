from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("desk", "0024_ticket_created_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="position",
            field=models.CharField(
                blank=True,
                default="",
                max_length=255,
                verbose_name="Должность",
            ),
        ),
    ]
