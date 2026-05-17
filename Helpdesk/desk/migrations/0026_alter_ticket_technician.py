from django.conf import settings
from django.db import migrations, models
import desk.models


class Migration(migrations.Migration):

    dependencies = [
        ("desk", "0025_userprofile_position"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="ticket",
            name="technician",
            field=models.ForeignKey(
                blank=True,
                default=desk.models.get_default_technician,
                limit_choices_to=models.Q(("groups__name", "Директор"), ("groups__name", "Исполнитель"), _connector="OR"),
                null=True,
                on_delete=models.PROTECT,
                related_name="assigned_tickets",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Исполнитель",
            ),
        ),
    ]
