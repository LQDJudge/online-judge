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
        f.writerow(["uid", "pid", "solved"])

        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM judge_problem")
            problem_ids = [row[0] for row in cursor.fetchall()]

        with connection.cursor() as cursor:
            for problem_id in problem_ids:
                cursor.execute(
                    """
                    SELECT user_id,
                           MAX(CASE WHEN result = 'AC' THEN 1 ELSE 0 END) as solved
                    FROM judge_submission
                    WHERE problem_id = %s AND user_id IS NOT NULL
                    GROUP BY user_id
                    """,
                    [problem_id],
                )
                for user_id, solved in cursor.fetchall():
                    f.writerow([user_id, problem_id, solved])
                    count += 1

    return count


def gen_users(output_path):
    print("Generating users")
    count = 0
    headers = ["uid", "username", "rating", "points", "problem_count"]
    with open(os.path.join(output_path, "profiles.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        f.writerow(headers)

        queryset = Profile.objects.values(
            "id", "user__username", "rating", "performance_points", "problem_count"
        ).iterator(chunk_size=5000)

        for u in queryset:
            f.writerow(
                [
                    u["id"],
                    u["user__username"],
                    u["rating"],
                    u["performance_points"],
                    u["problem_count"],
                ]
            )
            count += 1

    return count


def gen_problems(output_path):
    print("Generating problems")
    count = 0
    headers = [
        "pid",
        "code",
        "name",
        "points",
        "url",
        "ac_rate",
        "user_count",
        "group_id",
        "time_limit",
        "memory_limit",
    ]
    with open(os.path.join(output_path, "problems.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        f.writerow(headers)

        queryset = Problem.objects.values(
            "id",
            "code",
            "name",
            "points",
            "ac_rate",
            "user_count",
            "group__id",
            "time_limit",
            "memory_limit",
        ).iterator(chunk_size=5000)

        for p in queryset:
            f.writerow(
                [
                    p["id"],
                    p["code"],
                    p["name"],
                    p["points"],
                    "lqdoj.edu.vn/problem/" + p["code"],
                    p["ac_rate"],
                    p["user_count"],
                    p["group__id"] or 0,
                    p["time_limit"],
                    p["memory_limit"],
                ]
            )
            count += 1

    return count


def gen_problem_types(output_path):
    print("Generating problem types")
    count = 0
    with open(os.path.join(output_path, "problem_types.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        f.writerow(["pid", "type_id"])
        with connection.cursor() as cursor:
            cursor.execute("SELECT problem_id, problemtype_id FROM judge_problem_types")
            for pid, type_id in cursor.fetchall():
                f.writerow([pid, type_id])
                count += 1
    return count


def gen_problem_votes(output_path):
    print("Generating problem votes")
    count = 0
    with open(os.path.join(output_path, "problem_votes.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        f.writerow(["pid", "uid", "score"])
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT pv.object_id, pvv.voter_id, pvv.score
                FROM judge_pagevotevoter pvv
                JOIN judge_pagevote pv ON pv.id = pvv.pagevote_id
                JOIN django_content_type ct ON ct.id = pv.content_type_id
                WHERE ct.model = 'problem'
                """)
            for pid, uid, score in cursor.fetchall():
                f.writerow([pid, uid, score])
                count += 1
    return count


def gen_problem_bookmarks(output_path):
    print("Generating problem bookmarks")
    count = 0
    with open(os.path.join(output_path, "problem_bookmarks.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        f.writerow(["pid", "uid"])
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT bm.object_id, bu.profile_id
                FROM judge_bookmark_users bu
                JOIN judge_bookmark bm ON bm.id = bu.bookmark_id
                JOIN django_content_type ct ON ct.id = bm.content_type_id
                WHERE ct.model = 'problem'
                """)
            for pid, uid in cursor.fetchall():
                f.writerow([pid, uid])
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

        start = time.time()
        n = gen_problem_types(output_path)
        self.stdout.write(f"  -> {n} problem-type pairs in {time.time() - start:.2f}s")

        start = time.time()
        n = gen_problem_votes(output_path)
        self.stdout.write(f"  -> {n} problem votes in {time.time() - start:.2f}s")

        start = time.time()
        n = gen_problem_bookmarks(output_path)
        self.stdout.write(f"  -> {n} problem bookmarks in {time.time() - start:.2f}s")

        self.stdout.write(
            self.style.SUCCESS(f"Total time: {time.time() - total_start:.2f}s")
        )
        self.stdout.write(f"Output: {output_path}")
