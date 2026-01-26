from collections import defaultdict
from math import e
from datetime import datetime, timedelta
import random
from enum import Enum

from django.conf import settings
from django.core.cache import cache
from django.db.models import Case, Count, ExpressionWrapper, F, Max, Q, When
from django.db.models.fields import FloatField
from django.utils import timezone
from django.utils.translation import gettext as _, gettext_noop
from django.http import Http404

from judge.models import Problem, Submission
from judge.ml.collab_filter import CollabFilter
from judge.caching import cache_wrapper


__all__ = [
    "contest_completed_ids",
    "get_result_data",
    "user_completed_ids",
    "user_editable_ids",
    "user_tester_ids",
]


@cache_wrapper(prefix="user_tester")
def user_tester_ids(profile):
    return set(
        Problem.testers.through.objects.filter(profile=profile)
        .values_list("problem_id", flat=True)
        .distinct()
    )


@cache_wrapper(prefix="user_editable")
def user_editable_ids(profile):
    result = set(
        (
            Problem.objects.filter(authors=profile)
            | Problem.objects.filter(curators=profile)
        )
        .values_list("id", flat=True)
        .distinct()
    )
    return result


@cache_wrapper(prefix="contest_complete")
def contest_completed_ids(participation):
    result = set(
        participation.submissions.filter(
            submission__result="AC", points=F("problem__points")
        )
        .values_list("problem__problem__id", flat=True)
        .distinct()
    )
    return result


@cache_wrapper(prefix="user_complete")
def user_completed_ids(profile):
    result = set(
        Submission.objects.filter(
            user=profile, result="AC", points=F("problem__points")
        )
        .values_list("problem_id", flat=True)
        .distinct()
    )
    return result


@cache_wrapper(prefix="contest_attempted")
def contest_attempted_ids(participation):
    result = {
        id: {"achieved_points": points, "max_points": max_points}
        for id, max_points, points in (
            participation.submissions.values_list(
                "problem__problem__id", "problem__points"
            )
            .annotate(points=Max("points"))
            .filter(points__lt=F("problem__points"))
        )
    }
    return result


@cache_wrapper(prefix="user_attempted")
def user_attempted_ids(profile):
    result = {
        id: {
            "achieved_points": points,
            "max_points": max_points,
            "last_submission": last_submission,
            "code": problem_code,
            "name": problem_name,
        }
        for id, max_points, problem_code, problem_name, points, last_submission in (
            Submission.objects.filter(user=profile)
            .values_list(
                "problem__id", "problem__points", "problem__code", "problem__name"
            )
            .annotate(points=Max("points"), last_submission=Max("id"))
            .filter(points__lt=F("problem__points"))
        )
    }
    return result


def _get_result_data(results):
    return {
        "categories": [
            # Using gettext_noop here since this will be tacked into the cache, so it must be language neutral.
            # The caller, SubmissionList.get_result_data will run gettext on the name.
            {"code": "AC", "name": gettext_noop("Accepted"), "count": results["AC"]},
            {
                "code": "WA",
                "name": gettext_noop("Wrong Answer"),
                "count": results["WA"],
            },
            {
                "code": "CE",
                "name": gettext_noop("Compile Error"),
                "count": results["CE"],
            },
            {
                "code": "TLE",
                "name": gettext_noop("Time Limit Exceeded"),
                "count": results["TLE"],
            },
            {
                "code": "ERR",
                "name": gettext_noop("Error"),
                "count": results["MLE"]
                + results["OLE"]
                + results["IR"]
                + results["RTE"]
                + results["AB"]
                + results["IE"],
            },
        ],
        "total": sum(results.values()),
    }


def get_result_data(*args, **kwargs):
    if args:
        submissions = args[0]
        if kwargs:
            raise ValueError(_("Can't pass both queryset and keyword filters"))
    else:
        submissions = (
            Submission.objects.filter(**kwargs)
            if kwargs is not None
            else Submission.objects
        )
    raw = (
        submissions.values("result")
        .annotate(count=Count("result"))
        .values_list("result", "count")
    )
    return _get_result_data(defaultdict(int, raw))


def editable_problems(user, profile=None):
    subquery = Problem.objects.all()
    if profile is None:
        profile = user.profile
    if not user.has_perm("judge.edit_all_problem"):
        subfilter = Q(authors__id=profile.id) | Q(curators__id=profile.id)
        if user.has_perm("judge.edit_public_problem"):
            subfilter |= Q(is_public=True)
        subquery = subquery.filter(subfilter)
    return subquery


@cache_wrapper(prefix="hp", timeout=14400)
def hot_problems(duration, limit):
    qs = Problem.get_public_problems().filter(
        submission__date__gt=timezone.now() - duration
    )
    qs0 = (
        qs.annotate(k=Count("submission__user", distinct=True))
        .order_by("-k")
        .values_list("k", flat=True)
    )

    if not qs0:
        return []
    # make this an aggregate
    mx = float(qs0[0])

    qs = qs.annotate(unique_user_count=Count("submission__user", distinct=True))
    # fix braindamage in excluding CE
    qs = qs.annotate(
        submission_volume=Count(
            Case(
                When(submission__result="AC", then=1),
                When(submission__result="WA", then=1),
                When(submission__result="IR", then=1),
                When(submission__result="RTE", then=1),
                When(submission__result="TLE", then=1),
                When(submission__result="OLE", then=1),
                output_field=FloatField(),
            )
        )
    )
    qs = qs.annotate(
        ac_volume=Count(
            Case(
                When(submission__result="AC", then=1),
                output_field=FloatField(),
            )
        )
    )
    qs = qs.filter(unique_user_count__gt=max(mx / 3.0, 1))

    qs = (
        qs.annotate(
            ordering=ExpressionWrapper(
                0.02
                * F("points")
                * (0.4 * F("ac_volume") / F("submission_volume") + 0.6 * F("ac_rate"))
                + 100 * e ** (F("unique_user_count") / mx),
                output_field=FloatField(),
            )
        )
        .order_by("-ordering")
        .defer("description")[:limit]
    )
    return qs


@cache_wrapper(prefix="grp", timeout=14400)
def get_related_problems(profile, problem, limit=8):
    if not profile or not settings.ML_OUTPUT_PATH:
        return None
    problemset = Problem.get_visible_problems(profile.user).values_list("id", flat=True)
    problemset = problemset.exclude(id__in=user_completed_ids(profile))
    problemset = problemset.exclude(id=problem.id)

    results = []

    # Try two-tower model first
    try:
        from ml.two_tower.serving import get_recommender

        tt_model = get_recommender()
        if tt_model:
            results = tt_model.problem_neighbors(problem, list(problemset), limit * 2)
    except Exception:
        tt_model = None

    # Fall back to collaborative filter if two-tower not available
    if not results:
        cf_model = CollabFilter("collab_filter")
        results = cf_model.problem_neighbors(
            problem, problemset, CollabFilter.DOT, limit
        ) + cf_model.problem_neighbors(problem, problemset, CollabFilter.COSINE, limit)

    results = list(set([i[1] for i in results]))
    random.seed(datetime.now().strftime("%d%m%Y"))
    random.shuffle(results)
    results = results[:limit]
    return Problem.get_cached_instances(*results)


def finished_submission(sub, is_delete=False):
    keys = ["user_complete:%d" % sub.user_id, "user_attempted:%s" % sub.user_id]
    if hasattr(sub, "contest"):
        participation = sub.contest.participation
        keys += ["contest_complete:%d" % participation.id]
        keys += ["contest_attempted:%d" % participation.id]
    cache.delete_many(keys)

    # Update best submission cache for course lesson grade tracking
    from judge.models import BestSubmission

    if is_delete:
        # When deleting, recalculate best submission for this user/problem
        # The CASCADE delete will remove BestSubmission if it pointed to this submission,
        # so we need to find and set the new best submission from remaining ones
        BestSubmission.recalculate_for_user_problem(sub.user_id, sub.problem_id)
    else:
        BestSubmission.update_from_submission(sub)


class RecommendationType(Enum):
    HOT_PROBLEM = 1
    CF_DOT = 2
    CF_COSINE = 3
    CF_TIME_DOT = 4
    CF_TIME_COSINE = 5
    TWO_TOWER = 6


# Return a list of list. Each inner list correspond to each type in types
def get_user_recommended_problems(
    user_id,
    problem_ids,
    recommendation_types,
    limits,
    shuffle=False,
):
    cf_model = CollabFilter("collab_filter")
    cf_time_model = CollabFilter("collab_filter_time")

    # Lazy load two-tower model to avoid import errors if not installed
    two_tower_model = None

    def get_two_tower_model():
        nonlocal two_tower_model
        if two_tower_model is None:
            try:
                from ml.two_tower.serving import get_recommender

                two_tower_model = get_recommender()
            except Exception:
                two_tower_model = False  # Mark as unavailable
        return two_tower_model if two_tower_model else None

    def get_problem_ids_from_type(rec_type, limit):
        if type(rec_type) == int:
            try:
                rec_type = RecommendationType(rec_type)
            except ValueError:
                raise Http404()
        if rec_type == RecommendationType.HOT_PROBLEM:
            return [
                problem.id
                for problem in hot_problems(timedelta(days=7), limit)
                if problem.id in set(problem_ids)
            ]
        if rec_type == RecommendationType.CF_DOT:
            return cf_model.user_recommendations(
                user_id, problem_ids, cf_model.DOT, limit
            )
        if rec_type == RecommendationType.CF_COSINE:
            return cf_model.user_recommendations(
                user_id, problem_ids, cf_model.COSINE, limit
            )
        if rec_type == RecommendationType.CF_TIME_DOT:
            return cf_time_model.user_recommendations(
                user_id, problem_ids, cf_model.DOT, limit
            )
        if rec_type == RecommendationType.CF_TIME_COSINE:
            return cf_time_model.user_recommendations(
                user_id, problem_ids, cf_model.COSINE, limit
            )
        if rec_type == RecommendationType.TWO_TOWER:
            tt_model = get_two_tower_model()
            if tt_model:
                return tt_model.user_recommendations(user_id, problem_ids, limit)
            return []
        return []

    all_problems = []
    for rec_type, limit in zip(recommendation_types, limits):
        all_problems += get_problem_ids_from_type(rec_type, limit)
    if shuffle:
        seed = datetime.now().strftime("%d%m%Y")
        random.Random(seed).shuffle(all_problems)

    # deduplicate problems
    res = []
    used_pid = set()

    for obj in all_problems:
        if type(obj) == tuple:
            obj = obj[1]
        if obj not in used_pid:
            res.append(obj)
            used_pid.add(obj)
    return res
