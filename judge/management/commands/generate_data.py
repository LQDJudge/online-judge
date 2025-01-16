from django.core.management.base import BaseCommand
from judge.models import *
import csv
import os
from django.conf import settings
from django.db import connection
from collections import defaultdict


def gen_submissions():
    """
    Generate submissions using Django ORM's iterator and process data in Python.
    """
    print("Generating submissions")
    # Use defaultdict to group submissions by (user_id, problem_id)
    submissions_dict = defaultdict(lambda: None)

    # Iterate over Submissions ordered by -id (latest submissions first)
    queryset = Submission.objects.order_by("-id").iterator(chunk_size=10000)

    for submission in queryset:
        key = (submission.user_id, submission.problem_id)
        # Store the first (latest) submission for each user/problem pair
        if key not in submissions_dict:
            submissions_dict[key] = submission

    # Write the results to a CSV file
    with open(os.path.join(settings.ML_DATA_PATH, "submissions.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        # Write headers
        f.writerow(["user_id", "problem_id"])

        # Write rows from the dictionary
        for (user_id, problem_id), submission in submissions_dict.items():
            f.writerow([user_id, problem_id])


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
