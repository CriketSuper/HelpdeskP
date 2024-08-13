# Generated by Django 4.2 on 2023-05-13 04:19

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('desk', '0002_ticket_user_delete_application'),
    ]

    operations = [
        migrations.AlterField(
            model_name='ticket',
            name='criticalness',
            field=models.CharField(choices=[('Низкая', 'Низкая'), ('Средняя', 'Средняя'), ('Высокая', 'Высокая'), ('Критичная', 'Критичная')], default='Средняя', max_length=25, verbose_name='Критичность'),
        ),
        migrations.AlterField(
            model_name='ticket',
            name='progress',
            field=models.CharField(choices=[('Невозможно выполнить', 'Невозможно выполнить'), ('Принято к рассмотрению', 'Принято к рассмотрению'), ('В стадии решения', 'В стадии решения'), ('Успешно решено', 'Успешно решено')], default='Принято к рассмотрению', max_length=25, verbose_name='Статус заявки'),
        ),
        migrations.AlterField(
            model_name='user',
            name='telnumber',
            field=models.DecimalField(decimal_places=0, max_digits=11, verbose_name='Телефон'),
        ),
        migrations.AlterField(
            model_name='user',
            name='userclass',
            field=models.CharField(choices=[('1', 'Клиент'), ('2', 'Админ')], default='1', max_length=10, verbose_name='Класс пользователя'),
        ),
    ]
