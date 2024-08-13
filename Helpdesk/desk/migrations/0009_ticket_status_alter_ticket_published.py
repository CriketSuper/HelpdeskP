# Generated by Django 4.2 on 2023-05-20 15:00

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('desk', '0008_ticket_chat_alter_ticket_progress_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='ticket',
            name='status',
            field=models.CharField(choices=[('открыта', 'Открыта'), ('закрыта', 'Закрыта')], default='открыта', max_length=10, verbose_name='Статус заявки'),
        ),
        migrations.AlterField(
            model_name='ticket',
            name='published',
            field=models.DateTimeField(auto_now=True, db_index=True, verbose_name='Дата и время создания'),
        ),
    ]
