"""
Data migration to recalculate all BestQuizAttempt records.

After the quiz context isolation refactor, only lesson-scoped attempts
should count toward lesson progress. This migration recalculates all
cached best scores using the corrected logic.
"""

from django.db import migrations


def recalculate_best_quiz_attempts(apps, schema_editor):
    BestQuizAttempt = apps.get_model("judge", "BestQuizAttempt")
    QuizAttempt = apps.get_model("judge", "QuizAttempt")

    for bqa in BestQuizAttempt.objects.select_related("lesson_quiz").all():
        # Find the best submitted attempt for this user in this lesson context only
        best = (
            QuizAttempt.objects.filter(
                user_id=bqa.user_id,
                lesson_quiz_id=bqa.lesson_quiz_id,
                is_submitted=True,
            )
            .order_by("-score")
            .first()
        )

        if best:
            bqa.attempt = best
            bqa.score = best.score or 0
            bqa.max_score = best.max_score or 0
            bqa.save()
        else:
            # No lesson-scoped attempts exist — delete stale cache
            bqa.delete()


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("judge", "0231_make_problem_group_optional"),
    ]

    operations = [
        migrations.RunPython(recalculate_best_quiz_attempts, noop),
    ]
