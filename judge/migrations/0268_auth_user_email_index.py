from django.db import migrations, models

AUTH_USER_EMAIL_INDEX = "auth_user_email_idx"


def _has_index(schema_editor, model):
    with schema_editor.connection.cursor() as cursor:
        constraints = schema_editor.connection.introspection.get_constraints(
            cursor, model._meta.db_table
        )
    return AUTH_USER_EMAIL_INDEX in constraints


def add_auth_user_email_index(apps, schema_editor):
    User = apps.get_model("auth", "User")
    if not _has_index(schema_editor, User):
        schema_editor.add_index(
            User, models.Index(fields=["email"], name=AUTH_USER_EMAIL_INDEX)
        )


def remove_auth_user_email_index(apps, schema_editor):
    User = apps.get_model("auth", "User")
    if _has_index(schema_editor, User):
        schema_editor.remove_index(
            User, models.Index(fields=["email"], name=AUTH_USER_EMAIL_INDEX)
        )


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("judge", "0267_requestmetric_cache_call_count_and_more"),
    ]

    operations = [
        migrations.RunPython(add_auth_user_email_index, remove_auth_user_email_index),
    ]
