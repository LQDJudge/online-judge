from judge.models import SubmissionTestCase

from collections import defaultdict


def generate_report(problem):
    testcases = SubmissionTestCase.objects.filter(submission__problem=problem).all()

    score = defaultdict(int)
    total = defaultdict(int)
    rate = defaultdict(int)

    for case in testcases.iterator():
        score[case.case] += int(case.status == "AC")
        total[case.case] += 1

    for i in score:
        rate[i] = score[i] / total[i]

    for i, _ in sorted(rate.items(), key=lambda x: x[1], reverse=True):
        print(i, score[i], total[i], rate[i])
