from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("desk", "0026_alter_ticket_technician"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ticket",
            name="progress",
            field=models.CharField(
                choices=[
                    ("Невозможно выполнить", "Невозможно выполнить"),
                    ("Принято к рассмотрению", "Принято к рассмотрению"),
                    ("Согласовано", "Согласовано"),
                    ("К исполнению", "К исполнению"),
                    ("В стадии решения", "В стадии решения"),
                    ("Успешно решено", "Успешно решено"),
                ],
                default="Принято к рассмотрению",
                max_length=25,
                verbose_name="Статус заявки",
            ),
        ),
    ]
