# Generated by Django 4.2 on 2023-08-31 05:06

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('desk', '0017_document_ticket_documents'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='document',
            options={'verbose_name': 'Документ', 'verbose_name_plural': 'Документы'},
        ),
    ]
