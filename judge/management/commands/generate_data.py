from django.core.management.base import BaseCommand
from judge.models import *
from collections import defaultdict
import csv
import os
from django.conf import settings


def gen_submissions():
    headers = ['uid', 'pid']
    with open(os.path.join(settings.ML_DATA_PATH, 'submissions.csv'), 'w') as csvfile:
        f = csv.writer(csvfile)
        f.writerow(headers)
        
        last_pid = defaultdict(int)
        for u in Profile.objects.all():
            used = set()
            print('Processing user', u.id)
            for s in Submission.objects.filter(user=u).order_by('-date'):
                if s.problem.id not in used:
                    used.add(s.problem.id)
                    f.writerow([u.id, s.problem.id])

def gen_users():
    headers = ['uid', 'username', 'rating', 'points']
    with open(os.path.join(settings.ML_DATA_PATH, 'profiles.csv'), 'w') as csvfile:
        f = csv.writer(csvfile)
        f.writerow(headers)
        
        for u in Profile.objects.all():
            f.writerow([u.id, u.username, u.rating, u.performance_points])

def gen_problems():
    headers = ['pid', 'code', 'name', 'points', 'url']
    with open(os.path.join(settings.ML_DATA_PATH, 'problems.csv'), 'w') as csvfile:
        f = csv.writer(csvfile)
        f.writerow(headers)
        
        for p in Problem.objects.all():
            f.writerow([p.id, p.code, p.name, p.points, 'lqdoj.edu.vn/problem/' + p.code])


class Command(BaseCommand):
    help = 'generate data for ML'

    def handle(self, *args, **options):
        gen_users()
        gen_problems()
        gen_submissions()