# Generated by Django 3.2.18 on 2023-08-28 01:13

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("judge", "0165_drop_output_prefix_override"),
    ]

    operations = [
        migrations.AlterField(
            model_name="profile",
            name="display_rank",
            field=models.CharField(
                choices=[
                    ("user", "Normal User"),
                    ("setter", "Problem Setter"),
                    ("admin", "Admin"),
                ],
                db_index=True,
                default="user",
                max_length=10,
                verbose_name="display rank",
            ),
        ),
    ]
