from collections import defaultdict

from django.db.models import Count
from django.db.models import Q
from django.utils.translation import gettext_noop

from judge.models import BestSubmission, Contest, ContestSubmission
from judge.utils.problems import _get_result_data

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


def hidden_result_submission_ids(user):
    queryset = ContestSubmission.objects.filter(
        is_result_hidden=True,
        problem__problem__isnull=False,
    )
    if user.is_authenticated and user.has_perm("judge.edit_all_contest"):
        return queryset.none().values("submission_id")
    if user.is_authenticated:
        editable_contests = Contest.objects.filter(
            Q(authors=user.profile) | Q(curators=user.profile)
        ).values("id")
        queryset = queryset.exclude(problem__contest_id__in=editable_contests)
    return queryset.values("submission_id")


def filter_hidden_result_submissions(queryset, user):
    if user.is_authenticated and user.has_perm("judge.edit_all_contest"):
        return queryset.none()
    return queryset.filter(pk__in=hidden_result_submission_ids(user))


def exclude_hidden_result_submissions(queryset, user):
    if user.is_authenticated and user.has_perm("judge.edit_all_contest"):
        return queryset
    return queryset.exclude(pk__in=hidden_result_submission_ids(user))


def _get_result_counts(queryset):
    return defaultdict(
        int,
        queryset.values("result")
        .annotate(count=Count("result"))
        .values_list("result", "count"),
    )


def _subtract_result_counts(counts, hidden_counts):
    result = defaultdict(int, counts)
    for status, count in hidden_counts.items():
        result[status] -= count
    return result


def get_result_data_with_hidden(submissions, user):
    hidden_counts = _get_result_counts(
        filter_hidden_result_submissions(submissions, user)
    )
    result = _get_result_data(
        _subtract_result_counts(_get_result_counts(submissions), hidden_counts)
    )
    hidden_count = sum(hidden_counts.values())
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

    submission_ids = []
    for submission in submissions:
        if submission.id:
            submission_ids.append(submission.id)

    if not submission_ids:
        return

    queryset = ContestSubmission.objects.filter(
        submission_id__in=submission_ids,
        is_result_hidden=True,
        problem__problem__isnull=False,
    )
    if user.is_authenticated:
        editable_contests = Contest.objects.filter(
            Q(authors=user.profile) | Q(curators=user.profile)
        ).values("id")
        queryset = queryset.exclude(problem__contest_id__in=editable_contests)

    hidden_submission_ids = set(queryset.values_list("submission_id", flat=True))

    for submission in submissions:
        if submission.id in hidden_submission_ids:
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
