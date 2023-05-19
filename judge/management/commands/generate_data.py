from django.core.management.base import BaseCommand
from judge.models import *
from collections import defaultdict
import csv
import os
from django.conf import settings
from django.db import connection


def gen_submissions():
    print("Generating submissions")
    query = """
    SELECT user_id as uid, problem_id as pid from 
        (SELECT user_id, problem_id, max(date) as max_date 
            from judge_submission 
            group by user_id, problem_id) t 
            order by user_id, -max_date;
    """
    with connection.cursor() as cursor:
        cursor.execute(query)
        headers = [i[0] for i in cursor.description]
        with open(
            os.path.join(settings.ML_DATA_PATH, "submissions.csv"), "w"
        ) as csvfile:
            f = csv.writer(csvfile)
            f.writerow(headers)
            for row in cursor.fetchall():
                f.writerow(row)


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
