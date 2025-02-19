# Generated by Django 4.2 on 2023-06-14 17:09

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('desk', '0009_ticket_status_alter_ticket_published'),
    ]

    operations = [
        migrations.AlterField(
            model_name='ticket',
            name='progress',
            field=models.CharField(choices=[('Невозможно выполнить', 'Невозможно выполнить'), ('Принято к рассмотрению', 'Принято к рассмотрению'), ('В стадии решения', 'В стадии решения'), ('Успешно решено', 'Успешно решено')], default='Принято к рассмотрению', max_length=25, verbose_name='Статус заявки'),
        ),
    ]
