# Data migration to populate BestSubmission and BestQuizAttempt tables
# with existing best scores from Submission and QuizAttempt tables.
# Processes in batches by user to avoid query timeout on large databases.

from django.db import migrations, connection
import sys


def log(msg):
    """Print message with immediate flush for visibility during migrations."""
    print(msg)
    sys.stdout.flush()


def populate_best_submissions(apps, schema_editor):
    """
    Populate BestSubmission table with best submission per user/problem.
    Processes users in batches to avoid query timeout.
    """
    BestSubmission = apps.get_model("judge", "BestSubmission")
    Profile = apps.get_model("judge", "Profile")

    log("Starting BestSubmission population...")

    # Get all valid profile IDs
    profile_ids = list(Profile.objects.values_list("id", flat=True))
    total_profiles = len(profile_ids)
    log(f"  Found {total_profiles} profiles to process")

    cursor = connection.cursor()
    batch_size = 500  # Process 500 users at a time
    total_created = 0

    for i in range(0, total_profiles, batch_size):
        user_batch = profile_ids[i : i + batch_size]
        user_ids_str = ",".join(str(uid) for uid in user_batch)

        # Query best submissions for this batch of users
        cursor.execute(
            f"""
            SELECT s.id, s.user_id, s.problem_id, s.case_points, s.case_total
            FROM judge_submission s
            INNER JOIN (
                SELECT user_id, problem_id, MAX(case_points) as max_points
                FROM judge_submission
                WHERE status = 'D' AND case_total > 0 AND user_id IN ({user_ids_str})
                GROUP BY user_id, problem_id
            ) best ON s.user_id = best.user_id
                  AND s.problem_id = best.problem_id
                  AND s.case_points = best.max_points
            INNER JOIN (
                SELECT user_id, problem_id, case_points, MAX(id) as max_id
                FROM judge_submission
                WHERE status = 'D' AND case_total > 0 AND user_id IN ({user_ids_str})
                GROUP BY user_id, problem_id, case_points
            ) latest ON s.id = latest.max_id
            WHERE s.status = 'D' AND s.case_total > 0 AND s.user_id IN ({user_ids_str})
            """
        )

        rows = cursor.fetchall()

        if rows:
            records = [
                BestSubmission(
                    user_id=row[1],
                    problem_id=row[2],
                    submission_id=row[0],
                    points=row[3] or 0,
                    case_total=row[4] or 0,
                )
                for row in rows
            ]
            BestSubmission.objects.bulk_create(records, ignore_conflicts=True)
            total_created += len(records)

        progress = min(i + batch_size, total_profiles)
        log(
            f"  Progress: {progress}/{total_profiles} users processed, {total_created} records created"
        )

    log(f"Completed: Created {total_created} BestSubmission records")


def populate_best_quiz_attempts(apps, schema_editor):
    """
    Populate BestQuizAttempt table with best quiz attempt per user/lesson_quiz.
    Processes users in batches to avoid query timeout.
    """
    BestQuizAttempt = apps.get_model("judge", "BestQuizAttempt")
    Profile = apps.get_model("judge", "Profile")

    log("Starting BestQuizAttempt population...")

    # Get all valid profile IDs
    profile_ids = list(Profile.objects.values_list("id", flat=True))
    total_profiles = len(profile_ids)
    log(f"  Found {total_profiles} profiles to process")

    cursor = connection.cursor()
    batch_size = 1000  # Process 1000 users at a time (quiz attempts are fewer)
    total_created = 0

    for i in range(0, total_profiles, batch_size):
        user_batch = profile_ids[i : i + batch_size]
        user_ids_str = ",".join(str(uid) for uid in user_batch)

        # Query best quiz attempts for this batch of users
        cursor.execute(
            f"""
            SELECT a.id, a.user_id, a.lesson_quiz_id, a.score, a.max_score
            FROM judge_quizattempt a
            INNER JOIN (
                SELECT user_id, lesson_quiz_id, MAX(score) as max_score_val
                FROM judge_quizattempt
                WHERE is_submitted = 1 AND lesson_quiz_id IS NOT NULL
                      AND score IS NOT NULL AND user_id IN ({user_ids_str})
                GROUP BY user_id, lesson_quiz_id
            ) best ON a.user_id = best.user_id
                  AND a.lesson_quiz_id = best.lesson_quiz_id
                  AND a.score = best.max_score_val
            INNER JOIN (
                SELECT user_id, lesson_quiz_id, score, MAX(id) as max_id
                FROM judge_quizattempt
                WHERE is_submitted = 1 AND lesson_quiz_id IS NOT NULL
                      AND score IS NOT NULL AND user_id IN ({user_ids_str})
                GROUP BY user_id, lesson_quiz_id, score
            ) latest ON a.id = latest.max_id
            WHERE a.is_submitted = 1 AND a.lesson_quiz_id IS NOT NULL
                  AND a.score IS NOT NULL AND a.user_id IN ({user_ids_str})
            """
        )

        rows = cursor.fetchall()

        if rows:
            records = [
                BestQuizAttempt(
                    user_id=row[1],
                    lesson_quiz_id=row[2],
                    attempt_id=row[0],
                    score=row[3] or 0,
                    max_score=row[4] or 0,
                )
                for row in rows
            ]
            BestQuizAttempt.objects.bulk_create(records, ignore_conflicts=True)
            total_created += len(records)

        progress = min(i + batch_size, total_profiles)
        if progress % 5000 == 0 or progress == total_profiles:
            log(
                f"  Progress: {progress}/{total_profiles} users processed, {total_created} records created"
            )

    log(f"Completed: Created {total_created} BestQuizAttempt records")


def reverse_migrations(apps, schema_editor):
    """
    Reverse the data migration by clearing the tables.
    """
    BestSubmission = apps.get_model("judge", "BestSubmission")
    BestQuizAttempt = apps.get_model("judge", "BestQuizAttempt")

    BestSubmission.objects.all().delete()
    BestQuizAttempt.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("judge", "0221_course_prerequisites_feature"),
    ]

    operations = [
        migrations.RunPython(
            populate_best_submissions,
            reverse_code=reverse_migrations,
        ),
        migrations.RunPython(
            populate_best_quiz_attempts,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
