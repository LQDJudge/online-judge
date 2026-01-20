# Data migration to populate BestSubmission and BestQuizAttempt tables
# with existing best scores from Submission and QuizAttempt tables.
# Uses raw SQL for MySQL compatibility and bulk_create for performance.

from django.db import migrations, connection
import sys


def log(msg):
    """Print message with immediate flush for visibility during migrations."""
    print(msg)
    sys.stdout.flush()


def populate_best_submissions(apps, schema_editor):
    """
    Populate BestSubmission table with best submission per user/problem.
    Uses raw SQL for MySQL compatibility.
    """
    BestSubmission = apps.get_model("judge", "BestSubmission")
    Profile = apps.get_model("judge", "Profile")

    log("Starting BestSubmission population...")

    # Get valid profile IDs to filter out orphan submissions
    valid_profile_ids = set(Profile.objects.values_list("id", flat=True))
    log(f"  Found {len(valid_profile_ids)} valid profiles")

    log("Step 1: Finding best submissions for each user/problem pair using raw SQL...")

    # Use raw SQL to get the best submission for each user/problem pair
    # This query finds the submission with max case_points, and for ties, the latest one (max id)
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT s.id, s.user_id, s.problem_id, s.case_points, s.case_total
        FROM judge_submission s
        INNER JOIN (
            SELECT user_id, problem_id, MAX(case_points) as max_points
            FROM judge_submission
            WHERE status = 'D' AND case_total > 0
            GROUP BY user_id, problem_id
        ) best ON s.user_id = best.user_id
              AND s.problem_id = best.problem_id
              AND s.case_points = best.max_points
        INNER JOIN (
            SELECT user_id, problem_id, case_points, MAX(id) as max_id
            FROM judge_submission
            WHERE status = 'D' AND case_total > 0
            GROUP BY user_id, problem_id, case_points
        ) latest ON s.id = latest.max_id
        WHERE s.status = 'D' AND s.case_total > 0
    """
    )

    rows = cursor.fetchall()
    total_rows = len(rows)
    log(f"Step 2: Found {total_rows} best submissions to insert")

    # Filter to only valid profiles and bulk create in batches
    batch_size = 5000
    created_count = 0
    batch = []

    for row in rows:
        submission_id, user_id, problem_id, case_points, case_total = row

        # Skip if user no longer exists
        if user_id not in valid_profile_ids:
            continue

        batch.append(
            BestSubmission(
                user_id=user_id,
                problem_id=problem_id,
                submission_id=submission_id,
                points=case_points or 0,
                case_total=case_total or 0,
            )
        )

        if len(batch) >= batch_size:
            BestSubmission.objects.bulk_create(batch, ignore_conflicts=True)
            created_count += len(batch)
            log(f"  Progress: {created_count} records created...")
            batch = []

    # Insert remaining
    if batch:
        BestSubmission.objects.bulk_create(batch, ignore_conflicts=True)
        created_count += len(batch)

    log(f"Step 3: Created {created_count} BestSubmission records")


def populate_best_quiz_attempts(apps, schema_editor):
    """
    Populate BestQuizAttempt table with best quiz attempt per user/lesson_quiz.
    Uses raw SQL for MySQL compatibility.
    """
    BestQuizAttempt = apps.get_model("judge", "BestQuizAttempt")
    Profile = apps.get_model("judge", "Profile")

    log("Starting BestQuizAttempt population...")

    # Get valid profile IDs to filter out orphan attempts
    valid_profile_ids = set(Profile.objects.values_list("id", flat=True))
    log(f"  Found {len(valid_profile_ids)} valid profiles")

    log(
        "Step 1: Finding best quiz attempts for each user/lesson_quiz pair using raw SQL..."
    )

    # Use raw SQL to get the best attempt for each user/lesson_quiz pair
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT a.id, a.user_id, a.lesson_quiz_id, a.score, a.max_score
        FROM judge_quizattempt a
        INNER JOIN (
            SELECT user_id, lesson_quiz_id, MAX(score) as max_score_val
            FROM judge_quizattempt
            WHERE is_submitted = 1 AND lesson_quiz_id IS NOT NULL AND score IS NOT NULL
            GROUP BY user_id, lesson_quiz_id
        ) best ON a.user_id = best.user_id
              AND a.lesson_quiz_id = best.lesson_quiz_id
              AND a.score = best.max_score_val
        INNER JOIN (
            SELECT user_id, lesson_quiz_id, score, MAX(id) as max_id
            FROM judge_quizattempt
            WHERE is_submitted = 1 AND lesson_quiz_id IS NOT NULL AND score IS NOT NULL
            GROUP BY user_id, lesson_quiz_id, score
        ) latest ON a.id = latest.max_id
        WHERE a.is_submitted = 1 AND a.lesson_quiz_id IS NOT NULL AND a.score IS NOT NULL
    """
    )

    rows = cursor.fetchall()
    total_rows = len(rows)
    log(f"Step 2: Found {total_rows} best quiz attempts to insert")

    # Filter to only valid profiles and bulk create in batches
    batch_size = 1000
    created_count = 0
    batch = []

    for row in rows:
        attempt_id, user_id, lesson_quiz_id, score, max_score = row

        # Skip if user no longer exists
        if user_id not in valid_profile_ids:
            continue

        batch.append(
            BestQuizAttempt(
                user_id=user_id,
                lesson_quiz_id=lesson_quiz_id,
                attempt_id=attempt_id,
                score=score or 0,
                max_score=max_score or 0,
            )
        )

        if len(batch) >= batch_size:
            BestQuizAttempt.objects.bulk_create(batch, ignore_conflicts=True)
            created_count += len(batch)
            log(f"  Progress: {created_count} records created...")
            batch = []

    # Insert remaining
    if batch:
        BestQuizAttempt.objects.bulk_create(batch, ignore_conflicts=True)
        created_count += len(batch)

    log(f"Step 3: Created {created_count} BestQuizAttempt records")


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
