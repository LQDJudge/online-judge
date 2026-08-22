from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("judge", "0268_auth_user_email_index"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE judge_submissiontestcase "
                        "DROP COLUMN extended_feedback, "
                        "DROP COLUMN feedback, "
                        "DROP COLUMN output, "
                        "ALGORITHM=INSTANT, LOCK=NONE"
                    ),
                    # Result details were backfilled to result.json before this
                    # deploy; rolling back past this migration is a deploy
                    # boundary and should restore columns from backup if needed.
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
            state_operations=[
                migrations.RemoveField(
                    model_name="submissiontestcase",
                    name="extended_feedback",
                ),
                migrations.RemoveField(
                    model_name="submissiontestcase",
                    name="feedback",
                ),
                migrations.RemoveField(
                    model_name="submissiontestcase",
                    name="output",
                ),
            ],
        ),
    ]
