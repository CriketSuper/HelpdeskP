# Generated by Django 4.2 on 2023-08-30 06:49

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('desk', '0016_alter_ticket_title'),
    ]

    operations = [
        migrations.CreateModel(
            name='Document',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file', models.FileField(blank=True, null=True, upload_to='files/', verbose_name='Документ')),
                ('ticket', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='related_documents', to='desk.ticket', verbose_name='Заявка')),
            ],
        ),
        migrations.AddField(
            model_name='ticket',
            name='documents',
            field=models.ManyToManyField(blank=True, related_name='related_tickets', to='desk.document', verbose_name='Документ'),
        ),
    ]
