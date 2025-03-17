import csv
import re

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache

from celery import shared_task

from judge.models import Profile, Language, Organization
from judge.utils.celery import Progress


fields = ["username", "password", "name", "school", "email", "organizations"]
descriptions = [
    "my_username(edit old one if exist)",
    "123456 (must have)",
    "Le Van A (can be empty)",
    "Le Quy Don (can be empty)",
    "email@email.com (can be empty)",
    "org1&org2&org3&... (can be empty - org slug in URL)",
]


def csv_to_dict(csv_file):
    rows = csv.reader(csv_file.read().decode().split("\n"))
    header = next(rows)
    header = [i.lower() for i in header]

    if "username" not in header:
        return []

    res = []

    for row in rows:
        if len(row) != len(header):
            continue
        cur_dict = {i: "" for i in fields}
        for i in range(len(header)):
            if header[i] not in fields:
                continue
            cur_dict[header[i]] = row[i]
        if cur_dict["username"]:
            res.append(cur_dict)
    return res


def is_valid_username(username):
    match = re.match(r"\w+", username)
    return match is not None and match.group() == username


@shared_task(bind=True)
def import_users(self, users, profile_id=None):
    log = ""
    processed_count = 0

    with Progress(self, len(users), stage="Importing users") as progress:
        for i, row in enumerate(users):
            cur_log = str(i + 1) + ". "

            username = row["username"]
            if not is_valid_username(username):
                log += username + ": Invalid username\n"
                continue

            cur_log += username + ": "
            pwd = row["password"]
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "is_active": True,
                },
            )
            profile, _ = Profile.objects.get_or_create(
                user=user,
                defaults={
                    "language": Language.get_python3(),
                    "timezone": settings.DEFAULT_USER_TIME_ZONE,
                },
            )

            if created:
                cur_log += "Create new - "
            else:
                cur_log += "Edit - "

            if pwd:
                user.set_password(pwd)
            elif created:
                user.set_password("lqdoj")
                cur_log += "Missing password, set password = lqdoj - "

            if "name" in row.keys() and row["name"]:
                user.first_name = row["name"]

            if "school" in row.keys() and row["school"]:
                user.last_name = row["school"]

            if row["organizations"]:
                orgs = row["organizations"].split("&")
                added_orgs = []
                for o in orgs:
                    try:
                        org = Organization.objects.get(slug=o)
                        profile.organizations.add(org)
                        added_orgs.append(org.name)
                    except Organization.DoesNotExist:
                        continue
                if added_orgs:
                    cur_log += "Added to " + ", ".join(added_orgs) + " - "

            if row["email"]:
                user.email = row["email"]

            user.save()
            profile.save()
            processed_count += 1
            cur_log += "Saved\n"
            log += cur_log
            progress.did(1)

    log += "FINISH"

    # Store the log in cache if a profile_id was provided
    if profile_id:
        cache_key = f"import_users_log_{profile_id}"
        cache.set(cache_key, log, timeout=3600)  # Cache for 1 hour

    return processed_count
