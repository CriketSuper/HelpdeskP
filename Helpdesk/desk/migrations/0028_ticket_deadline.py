from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("desk", "0027_alter_ticket_progress"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="deadline",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Срок выполнения"),
        ),
    ]
