from django.core.management.base import BaseCommand
from judge.models import *
import csv
import os
from django.conf import settings
from django.db import connection
from collections import defaultdict


def gen_submissions():
    print("Generating submissions")
    batch_size = 10000  # Limit for each batch
    last_id = None  # Track the last processed ID
    submissions_dict = defaultdict(lambda: None)

    while True:
        # Fetch a batch of submissions ordered by descending ID
        if last_id is None:
            queryset = Submission.objects.order_by("-id").values(
                "id", "user_id", "problem_id"
            )[:batch_size]
        else:
            queryset = (
                Submission.objects.filter(id__lt=last_id)
                .order_by("-id")
                .values("id", "user_id", "problem_id")[:batch_size]
            )

        # Convert queryset to a list
        submissions = list(queryset)
        if not submissions:
            break  # Exit the loop if no more submissions are left

        # Process the batch
        for submission in submissions:
            key = (submission["user_id"], submission["problem_id"])
            # Store the first (latest) submission for each user/problem pair
            if key not in submissions_dict:
                submissions_dict[key] = submission

        # Update last_id for the next batch
        last_id = submissions[-1]["id"]

    # Write the results to a CSV file
    with open(os.path.join(settings.ML_DATA_PATH, "submissions.csv"), "w") as csvfile:
        f = csv.writer(csvfile)
        # Write headers
        f.writerow(["uid", "pid"])

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
