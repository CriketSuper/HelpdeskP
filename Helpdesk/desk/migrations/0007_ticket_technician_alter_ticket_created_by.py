# Generated by Django 4.2 on 2023-05-19 09:33

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('desk', '0006_alter_ticket_options'),
    ]

    operations = [
        migrations.AddField(
            model_name='ticket',
            name='technician',
            field=models.ForeignKey(blank=True, limit_choices_to={'groups__name': 'Техник'}, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='assigned_tickets', to=settings.AUTH_USER_MODEL, verbose_name='Исполнитель'),
        ),
        migrations.AlterField(
            model_name='ticket',
            name='created_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='created_tickets', to=settings.AUTH_USER_MODEL, verbose_name='Автор'),
        ),
    ]
