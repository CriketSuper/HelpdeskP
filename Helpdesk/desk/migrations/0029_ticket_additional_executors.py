from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("desk", "0028_ticket_deadline"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="additional_executors",
            field=models.ManyToManyField(
                blank=True,
                limit_choices_to=models.Q(("groups__name", "Директор"), ("groups__name", "Исполнитель"), _connector="OR"),
                related_name="co_assigned_tickets",
                to="auth.user",
                verbose_name="Соисполнители",
            ),
        ),
    ]
