# Data migration to populate BestSubmission and BestQuizAttempt tables
# with existing best scores from Submission and QuizAttempt tables.
# Processes ONE user at a time to avoid query timeout on large databases.

from django.db import migrations, connection
import sys


def log(msg):
    """Print message with immediate flush for visibility during migrations."""
    print(msg)
    sys.stdout.flush()


def populate_best_submissions(apps, schema_editor):
    """
    Populate BestSubmission table with best submission per user/problem.
    Processes ONE user at a time to avoid query timeout.
    """
    BestSubmission = apps.get_model("judge", "BestSubmission")
    Profile = apps.get_model("judge", "Profile")

    log("Starting BestSubmission population...")

    # Get all valid profile IDs
    profile_ids = list(Profile.objects.values_list("id", flat=True))
    total_profiles = len(profile_ids)
    log(f"  Found {total_profiles} profiles to process")

    cursor = connection.cursor()
    total_created = 0
    batch_records = []
    batch_size = 1000  # Bulk insert every 1000 records

    for idx, user_id in enumerate(profile_ids):
        # Simple query for ONE user - should be fast with index on user_id
        cursor.execute(
            """
            SELECT id, problem_id, case_points, case_total
            FROM judge_submission
            WHERE user_id = %s AND status = 'D' AND case_total > 0
            ORDER BY problem_id, case_points DESC, id DESC
            """,
            [user_id],
        )

        rows = cursor.fetchall()

        # Find best submission per problem for this user
        seen_problems = set()
        for row in rows:
            submission_id, problem_id, case_points, case_total = row
            if problem_id not in seen_problems:
                seen_problems.add(problem_id)
                batch_records.append(
                    BestSubmission(
                        user_id=user_id,
                        problem_id=problem_id,
                        submission_id=submission_id,
                        points=case_points or 0,
                        case_total=case_total or 0,
                    )
                )

        # Bulk insert when batch is full
        if len(batch_records) >= batch_size:
            BestSubmission.objects.bulk_create(batch_records, ignore_conflicts=True)
            total_created += len(batch_records)
            batch_records = []

        # Progress every 1000 users
        if (idx + 1) % 1000 == 0 or idx + 1 == total_profiles:
            log(
                f"  Progress: {idx + 1}/{total_profiles} users, {total_created + len(batch_records)} records"
            )

    # Insert remaining
    if batch_records:
        BestSubmission.objects.bulk_create(batch_records, ignore_conflicts=True)
        total_created += len(batch_records)

    log(f"Completed: Created {total_created} BestSubmission records")


def populate_best_quiz_attempts(apps, schema_editor):
    """
    Populate BestQuizAttempt table with best quiz attempt per user/lesson_quiz.
    Processes ONE user at a time to avoid query timeout.
    """
    BestQuizAttempt = apps.get_model("judge", "BestQuizAttempt")
    Profile = apps.get_model("judge", "Profile")

    log("Starting BestQuizAttempt population...")

    # Get all valid profile IDs
    profile_ids = list(Profile.objects.values_list("id", flat=True))
    total_profiles = len(profile_ids)
    log(f"  Found {total_profiles} profiles to process")

    cursor = connection.cursor()
    total_created = 0
    batch_records = []
    batch_size = 1000

    for idx, user_id in enumerate(profile_ids):
        # Simple query for ONE user
        cursor.execute(
            """
            SELECT id, lesson_quiz_id, score, max_score
            FROM judge_quizattempt
            WHERE user_id = %s AND is_submitted = 1
                  AND lesson_quiz_id IS NOT NULL AND score IS NOT NULL
            ORDER BY lesson_quiz_id, score DESC, id DESC
            """,
            [user_id],
        )

        rows = cursor.fetchall()

        # Find best attempt per lesson_quiz for this user
        seen_quizzes = set()
        for row in rows:
            attempt_id, lesson_quiz_id, score, max_score = row
            if lesson_quiz_id not in seen_quizzes:
                seen_quizzes.add(lesson_quiz_id)
                batch_records.append(
                    BestQuizAttempt(
                        user_id=user_id,
                        lesson_quiz_id=lesson_quiz_id,
                        attempt_id=attempt_id,
                        score=score or 0,
                        max_score=max_score or 0,
                    )
                )

        # Bulk insert when batch is full
        if len(batch_records) >= batch_size:
            BestQuizAttempt.objects.bulk_create(batch_records, ignore_conflicts=True)
            total_created += len(batch_records)
            batch_records = []

        # Progress every 5000 users
        if (idx + 1) % 5000 == 0 or idx + 1 == total_profiles:
            log(
                f"  Progress: {idx + 1}/{total_profiles} users, {total_created + len(batch_records)} records"
            )

    # Insert remaining
    if batch_records:
        BestQuizAttempt.objects.bulk_create(batch_records, ignore_conflicts=True)
        total_created += len(batch_records)

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
