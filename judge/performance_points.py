from collections import namedtuple

from django.conf import settings
from django.utils.timezone import is_naive

from judge.models import BestSubmission, Submission
from judge.timezone import from_database_time

PP_WEIGHT_TABLE = [
    pow(settings.DMOJ_PP_STEP, i) for i in range(settings.DMOJ_PP_ENTRIES)
]

PPBreakdown = namedtuple(
    "PPBreakdown",
    "points weight scaled_points problem_name problem_code "
    "sub_id sub_date sub_points sub_total sub_result_class "
    "sub_short_status sub_long_status sub_lang",
)


def _submission_date_display(date):
    if is_naive(date):
        return from_database_time(date)
    return date


def get_pp_breakdown(user, start=0, end=settings.DMOJ_PP_ENTRIES):
    best_submissions = list(
        BestSubmission.objects.filter(
            user=user,
            problem__is_public=True,
            problem__is_organization_private=False,
            submission__points__isnull=False,
            submission__points__gt=0,
        )
        .values(
            "problem__code",
            "problem__name",
            "submission_id",
            "submission__date",
            "submission__points",
            "submission__case_points",
            "submission__case_total",
            "submission__result",
            "submission__language__short_name",
            "submission__language__key",
        )
        .order_by("-submission__points", "-submission__date", "-submission__id")[
            start : end + 1
        ]
    )

    breakdown = []
    for weight, best_submission in zip(PP_WEIGHT_TABLE[start:end], best_submissions):
        # Replicates a lot of the logic usually done on Submission objects
        lang_short_display_name = (
            best_submission["submission__language__short_name"]
            or best_submission["submission__language__key"]
        )
        result_class = Submission.result_class_from_code(
            best_submission["submission__result"],
            best_submission["submission__case_points"],
            best_submission["submission__case_total"],
        )
        long_status = Submission.USER_DISPLAY_CODES.get(
            best_submission["submission__result"], ""
        )

        breakdown.append(
            PPBreakdown(
                points=best_submission["submission__points"],
                weight=weight * 100,
                scaled_points=best_submission["submission__points"] * weight,
                problem_name=best_submission["problem__name"],
                problem_code=best_submission["problem__code"],
                sub_id=best_submission["submission_id"],
                sub_date=_submission_date_display(best_submission["submission__date"]),
                sub_points=best_submission["submission__case_points"],
                sub_total=best_submission["submission__case_total"],
                sub_short_status=best_submission["submission__result"],
                sub_long_status=long_status,
                sub_result_class=result_class,
                sub_lang=lang_short_display_name,
            )
        )
    has_more = end < min(len(PP_WEIGHT_TABLE), start + len(best_submissions))
    return breakdown, has_more
