import csv
import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from judge.models import Problem, Profile


def gen_submissions():
    print("Generating submissions")

    # Use raw SQL for efficiency - single query with DISTINCT
    # This avoids loading all submissions into memory and repeated queries
    query = """
        SELECT DISTINCT user_id, problem_id
        FROM judge_submission
        WHERE user_id IS NOT NULL
    """

    with open(os.path.join(settings.ML_DATA_PATH, "submissions.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        f.writerow(["uid", "pid"])

        # Use server-side cursor for memory efficiency
        with connection.cursor() as cursor:
            cursor.execute(query)

            # Fetch and write in batches to avoid memory issues
            batch_size = 10000
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    f.writerow(row)


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
