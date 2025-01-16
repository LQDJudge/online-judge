from django.core.management.base import BaseCommand
from judge.models import *
import csv
import os
from django.conf import settings
from django.db import connection


def gen_submissions():
    print("Generating submissions")
    batch_size = 5000  # Limit for each batch
    offset = 0  # Start offset
    query_template = """
    SELECT user_id as uid, problem_id as pid FROM 
        (SELECT user_id, problem_id, max(date) as max_date 
            FROM judge_submission 
            GROUP BY user_id, problem_id) t 
            ORDER BY user_id, -max_date 
    LIMIT {limit} OFFSET {offset};
    """
    with connection.cursor() as cursor:
        headers_written = False
        with open(
            os.path.join(settings.ML_DATA_PATH, "submissions.csv"), "w"
        ) as csvfile:
            f = csv.writer(csvfile)

            while True:
                query = query_template.format(limit=batch_size, offset=offset)
                cursor.execute(query)
                rows = cursor.fetchall()

                # Write headers only once
                if not headers_written:
                    headers = [i[0] for i in cursor.description]
                    f.writerow(headers)
                    headers_written = True

                if not rows:
                    # No more data to fetch
                    break

                for row in rows:
                    f.writerow(row)

                # Increment offset for the next batch
                offset += batch_size


def gen_users():
    print("Generating users")
    headers = ["uid", "username", "rating", "points"]
    with open(os.path.join(settings.ML_DATA_PATH, "profiles.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        f.writerow(headers)

        for u in Profile.objects.all().iterator():
            f.writerow([u.id, u.username, u.rating, u.performance_points])


def gen_problems():
    print("Generating problems")
    headers = ["pid", "code", "name", "points", "url"]
    with open(os.path.join(settings.ML_DATA_PATH, "problems.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        f.writerow(headers)
        for p in Problem.objects.all().iterator():
            f.writerow(
                [p.id, p.code, p.name, p.points, "lqdoj.edu.vn/problem/" + p.code]
            )


class Command(BaseCommand):
    help = "generate data for ML"

    def handle(self, *args, **options):
        gen_users()
        gen_problems()
        gen_submissions()
