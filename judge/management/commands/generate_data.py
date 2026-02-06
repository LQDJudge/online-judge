import csv
import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from judge.models import Problem, Profile


def gen_submissions():
    print("Generating submissions")

    # Process per-problem to avoid long-running queries that timeout
    # Each problem query is fast and uses the (problem_id, user_id) index
    with open(os.path.join(settings.ML_DATA_PATH, "submissions.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        f.writerow(["uid", "pid"])

        # Get all problem IDs first
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM judge_problem")
            problem_ids = [row[0] for row in cursor.fetchall()]

        # For each problem, get distinct users who submitted
        with connection.cursor() as cursor:
            for problem_id in problem_ids:
                cursor.execute(
                    """
                    SELECT DISTINCT user_id
                    FROM judge_submission
                    WHERE problem_id = %s AND user_id IS NOT NULL
                    """,
                    [problem_id],
                )
                for (user_id,) in cursor.fetchall():
                    f.writerow([user_id, problem_id])


def gen_users():
    print("Generating users")
    headers = ["uid", "username", "rating", "points"]
    with open(os.path.join(settings.ML_DATA_PATH, "profiles.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        f.writerow(headers)

        # Use values() to only fetch needed fields, with iterator for memory efficiency
        queryset = Profile.objects.values(
            "id", "user__username", "rating", "performance_points"
        ).iterator(chunk_size=5000)

        for u in queryset:
            f.writerow(
                [u["id"], u["user__username"], u["rating"], u["performance_points"]]
            )


def gen_problems():
    print("Generating problems")
    headers = ["pid", "code", "name", "points", "url"]
    with open(os.path.join(settings.ML_DATA_PATH, "problems.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        f.writerow(headers)

        # Use values() to only fetch needed fields
        queryset = Problem.objects.values("id", "code", "name", "points").iterator(
            chunk_size=5000
        )

        for p in queryset:
            f.writerow(
                [
                    p["id"],
                    p["code"],
                    p["name"],
                    p["points"],
                    "lqdoj.edu.vn/problem/" + p["code"],
                ]
            )


class Command(BaseCommand):
    help = "generate data for ML"

    def handle(self, *args, **options):
        total_start = time.time()

        start = time.time()
        gen_users()
        self.stdout.write(f"  -> Completed in {time.time() - start:.2f}s")

        start = time.time()
        gen_problems()
        self.stdout.write(f"  -> Completed in {time.time() - start:.2f}s")

        start = time.time()
        gen_submissions()
        self.stdout.write(f"  -> Completed in {time.time() - start:.2f}s")

        self.stdout.write(
            self.style.SUCCESS(f"Total time: {time.time() - total_start:.2f}s")
        )
