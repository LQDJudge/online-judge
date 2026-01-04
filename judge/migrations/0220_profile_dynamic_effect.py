from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("judge", "0219_quiz_visibility_and_grading"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="dynamic_effect",
            field=models.CharField(
                choices=[
                    ("none", "None"),
                    ("snowflakes", "Snowflakes"),
                    ("cherry_blossoms", "Cherry Blossoms"),
                    ("rain", "Rain"),
                    ("stars", "Stars"),
                    ("fireflies", "Fireflies"),
                ],
                default="none",
                max_length=30,
                verbose_name="Dynamic effect",
            ),
        ),
    ]
