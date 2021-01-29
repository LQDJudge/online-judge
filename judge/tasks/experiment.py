from django.contrib.auth.models import User
from django.conf import settings

from judge.models import SubmissionTestCase, Problem, Profile, Language, Organization

from collections import defaultdict
import csv

def generate_report(problem):
    testcases = SubmissionTestCase.objects.filter(submission__problem=problem).all()
    
    score = defaultdict(int)
    total = defaultdict(int)
    rate = defaultdict(int)

    for case in testcases.iterator():
        score[case.case] += int(case.status == 'AC')
        total[case.case] += 1

    for i in score:
        rate[i] = score[i] / total[i]

    for i, _ in sorted(rate.items(), key=lambda x: x[1], reverse=True):
        print(i, score[i], total[i], rate[i])


def import_users(csv_file):
    # 1st row: username, password, organization
    # ... row: a_username,passhere,organ
    try:
        f = open(csv_file, 'r')
    except OSError:
        print("Could not open csv file", csv_file)
        return

    with f:
        reader = csv.DictReader(f)

        for row in reader:
            username = row['username']
            pwd = row['password']

            user, _ = User.objects.get_or_create(username=username, defaults={
                'is_active': True,
            })

            profile, _ = Profile.objects.get_or_create(user=user, defaults={
                'language': Language.get_python3(),
                'timezone': settings.DEFAULT_USER_TIME_ZONE,
            })

            if pwd:
                user.set_password(pwd)
            
            if 'organization' in row.keys() and row['organization']:
                org = Organization.objects.get(name=row['organization'])
                profile.organizations.add(org)

            user.save()
            profile.save()
