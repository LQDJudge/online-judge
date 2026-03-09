import csv
import os
import time

from django.core.management.base import BaseCommand
from django.db import connection

from judge.models import Problem, Profile


def gen_submissions(output_path):
    print("Generating submissions")
    count = 0

    # Process per-problem to avoid long-running queries that timeout (8s).
    # Each problem query is fast and uses the (problem_id, user_id) index.
    with open(os.path.join(output_path, "submissions.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        f.writerow(["uid", "pid"])

        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM judge_problem")
            problem_ids = [row[0] for row in cursor.fetchall()]

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
                    count += 1

    return count


def gen_users(output_path):
    print("Generating users")
    count = 0
    headers = ["uid", "username", "rating", "points"]
    with open(os.path.join(output_path, "profiles.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        f.writerow(headers)

        queryset = Profile.objects.values(
            "id", "user__username", "rating", "performance_points"
        ).iterator(chunk_size=5000)

        for u in queryset:
            f.writerow(
                [u["id"], u["user__username"], u["rating"], u["performance_points"]]
            )
            count += 1

    return count


def gen_problems(output_path):
    print("Generating problems")
    count = 0
    headers = ["pid", "code", "name", "points", "url"]
    with open(os.path.join(output_path, "problems.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        f.writerow(headers)

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
            count += 1

    return count


class Command(BaseCommand):
    help = "Generate CSV data for ML training"

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            type=str,
            required=True,
            help="Output directory for CSV files",
        )

    def handle(self, *args, **options):
        output_path = options["output"]
        os.makedirs(output_path, exist_ok=True)
        total_start = time.time()

        start = time.time()
        n = gen_users(output_path)
        self.stdout.write(f"  -> {n} users in {time.time() - start:.2f}s")

        start = time.time()
        n = gen_problems(output_path)
        self.stdout.write(f"  -> {n} problems in {time.time() - start:.2f}s")

        start = time.time()
        n = gen_submissions(output_path)
        self.stdout.write(f"  -> {n} submissions in {time.time() - start:.2f}s")

        self.stdout.write(
            self.style.SUCCESS(f"Total time: {time.time() - total_start:.2f}s")
        )
        self.stdout.write(f"Output: {output_path}")
