from collections import defaultdict

from django.db.models import F, Q
from django.utils.translation import gettext_noop

from judge.models import BestSubmission, Contest, ContestProblem, Submission
from judge.utils.problems import get_result_data

HIDDEN_RESULT_STATUS = "HIDDEN"


def format_data_key(contest_problem):
    if contest_problem.quiz_id:
        return f"quiz_{contest_problem.id}"
    return str(contest_problem.id)


def hidden_result_problem_ids(contest, user):
    if contest.is_editable_by(user):
        return set()
    return set(
        contest.contest_problems.filter(
            is_result_hidden=True, problem__isnull=False
        ).values_list("problem_id", flat=True)
    )


def hidden_result_contest_problem_ids(contest, user):
    if contest.is_editable_by(user):
        return set()
    return set(
        contest.contest_problems.filter(is_result_hidden=True).values_list(
            "id", flat=True
        )
    )


def hidden_result_format_keys(contest, user):
    if contest.is_editable_by(user):
        return set()
    return {
        format_data_key(contest_problem)
        for contest_problem in contest.contest_problems.filter(
            is_result_hidden=True
        ).only("id", "quiz")
    }


def is_problem_result_hidden(contest, problem_id, user):
    if not contest or contest.is_editable_by(user):
        return False
    return contest.contest_problems.filter(
        problem_id=problem_id, is_result_hidden=True
    ).exists()


def hidden_result_submission_filter(user):
    if user.is_authenticated and user.has_perm("judge.edit_all_contest"):
        return Q(pk__in=[])

    hidden_filter = Q(
        contest_object__contest_problems__is_result_hidden=True,
        contest_object__contest_problems__problem_id=F("problem_id"),
    )
    if user.is_authenticated:
        editable_contests = Contest.objects.filter(
            Q(authors=user.profile) | Q(curators=user.profile)
        ).values("id")
        hidden_filter &= ~Q(contest_object_id__in=editable_contests)
    return hidden_filter


def hidden_result_submission_ids(user):
    return Submission.objects.filter(hidden_result_submission_filter(user)).values("id")


def get_result_data_with_hidden(submissions, user):
    hidden_ids = hidden_result_submission_ids(user)
    result = get_result_data(submissions.exclude(id__in=hidden_ids).order_by())
    hidden_count = submissions.filter(id__in=hidden_ids).count()
    if hidden_count:
        result["categories"].append(
            {
                "code": HIDDEN_RESULT_STATUS,
                "name": gettext_noop("Hidden"),
                "count": hidden_count,
            }
        )
        result["total"] += hidden_count
    return result


def mark_submission_result_hidden(submission):
    setattr(submission, "_is_result_hidden", True)
    if submission.status in ("IE", "CE", "AB"):
        setattr(submission, "_result_class", submission.result_class)
    else:
        setattr(submission, "_result_class", "TLE")


def mark_hidden_result_submissions(submissions, user):
    if user.is_authenticated and user.has_perm("judge.edit_all_contest"):
        return

    contest_problem_pairs = defaultdict(set)
    for submission in submissions:
        if submission.contest_object_id:
            contest_problem_pairs[submission.contest_object_id].add(
                submission.problem_id
            )

    if not contest_problem_pairs:
        return

    queryset = ContestProblem.objects.filter(
        contest_id__in=contest_problem_pairs.keys(),
        is_result_hidden=True,
        problem__isnull=False,
    )
    if user.is_authenticated:
        editable_contests = Contest.objects.filter(
            Q(authors=user.profile) | Q(curators=user.profile)
        ).values("id")
        queryset = queryset.exclude(contest_id__in=editable_contests)

    hidden_pairs = {
        (contest_id, problem_id)
        for contest_id, problem_id in queryset.values_list("contest_id", "problem_id")
    }

    for submission in submissions:
        if (submission.contest_object_id, submission.problem_id) in hidden_pairs:
            mark_submission_result_hidden(submission)


def hidden_result_best_submission_problem_ids(profile, user):
    best_submissions = list(
        BestSubmission.objects.filter(
            user=profile, submission__contest_object_id__isnull=False
        ).select_related("submission")
    )
    mark_hidden_result_submissions(
        [best.submission for best in best_submissions if best.submission], user
    )
    return {
        best.problem_id
        for best in best_submissions
        if best.submission and getattr(best.submission, "_is_result_hidden", False)
    }
