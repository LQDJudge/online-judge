# Generated by Django 4.2.17 on 2025-01-15 01:11

from django.db import migrations, models
import django.utils.timezone


def drop_all_rows_from_old_table(apps, schema_editor):
    OrganizationProfile = apps.get_model("judge", "OrganizationProfile")
    OrganizationProfile.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("judge", "0202_add_index_profile_last_access"),
    ]

    operations = [
        migrations.RunPython(
            drop_all_rows_from_old_table,
            reverse_code=migrations.RunPython.noop,  # No reverse operation
        ),
        migrations.RemoveField(
            model_name="organizationprofile",
            name="last_visit",
        ),
        migrations.AddField(
            model_name="organizationprofile",
            name="id",
            field=models.AutoField(
                auto_created=True,
                primary_key=True,
                serialize=False,
                verbose_name="ID",
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="organizationprofile",
            name="last_visit_time",
            field=models.DateTimeField(
                db_index=True,
                default=django.utils.timezone.now,
                verbose_name="last visit",
            ),
        ),
    ]
