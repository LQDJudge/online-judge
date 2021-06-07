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
    # 1st row: username, password, name, organization
    # ... row: a_username, passhere, my_name, organ
    try:
        f = open(csv_file, 'r')
    except OSError:
        print("Could not open csv file", csv_file)
        return

    with f:
        reader = csv.DictReader(f)

        for row in reader:
            try:
                username = row['username']
                pwd = row['password']
            except Exception:
                print('username and/or password column missing')
                print('Make sure your columns are: username, password, name, organization')
            
            user, created = User.objects.get_or_create(username=username, defaults={
                'is_active': True,
            })

            profile, _ = Profile.objects.get_or_create(user=user, defaults={
                'language': Language.get_python3(),
                'timezone': settings.DEFAULT_USER_TIME_ZONE,
            })
            if created:
                print('Created user', username)

            if pwd:
                user.set_password(pwd)
            elif created:
                user.set_password('lqdoj')
                print('User', username, 'missing password, default=lqdoj')

            if 'name' in row.keys() and row['name']:
                user.first_name = row['name']

            if 'organization' in row.keys() and row['organization']:
                org = Organization.objects.get(name=row['organization'])
                profile.organizations.add(org)
            user.email = row['email']
            user.save()
            profile.save()
