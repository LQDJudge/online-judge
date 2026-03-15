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
from judge.models.course import BestSubmission
from judge.ml.vector_store import VectorStore
from judge.caching import cache_wrapper


__all__ = [
    "contest_completed_ids",
    "get_result_data",
    "user_completed_ids",
    "user_editable_ids",
    "user_tester_ids",
]


@cache_wrapper(prefix="user_tester_v2")
def user_tester_ids(profile):
    return set(
        Problem.testers.through.objects.filter(profile=profile)
        .values_list("problem_id", flat=True)
        .distinct()
    )


@cache_wrapper(prefix="user_editable_v2")
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
        BestSubmission.objects.filter(
            user=profile, points__gte=F("case_total"), case_total__gt=0
        ).values_list("problem_id", flat=True)
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
        bs["problem_id"]: {
            "achieved_points": bs["points"],
            "max_points": bs["case_total"],
            "last_submission": bs["submission_id"],
            "code": bs["problem__code"],
            "name": bs["problem__name"],
        }
        for bs in BestSubmission.objects.filter(user=profile)
        .exclude(points__gte=F("case_total"), case_total__gt=0)
        .values(
            "problem_id",
            "problem__code",
            "problem__name",
            "points",
            "case_total",
            "submission_id",
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
    if not profile or not getattr(settings, "USE_ML", False):
        return None
    problemset = Problem.get_visible_problems(profile.user).values_list("id", flat=True)
    problemset = problemset.exclude(id__in=user_completed_ids(profile))
    problemset = problemset.exclude(id=problem.id)

    two_tower_model = VectorStore("two_tower")
    results = two_tower_model.problem_neighbors(problem, problemset, limit * 2)
    if not results:
        cf_model = VectorStore("collab_filter")
        results = cf_model.problem_neighbors(problem, problemset, limit * 2)

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

    if sub.result == "AC":
        # Avoid circular import: contest_recommendation imports user_completed_ids from here
        from judge.utils.contest_recommendation import (
            get_recommended_contests,
            _get_user_skill,
        )

        get_recommended_contests.dirty(sub.user)  # sub.user is the Profile object
        _get_user_skill.dirty(sub.user)

    # Update best submission cache for course lesson grade tracking
    if is_delete:
        # When deleting, recalculate best submission for this user/problem
        # The CASCADE delete will remove BestSubmission if it pointed to this submission,
        # so we need to find and set the new best submission from remaining ones
        BestSubmission.recalculate_for_user_problem(sub.user_id, sub.problem_id)
    else:
        BestSubmission.update_from_submission(sub)


class RecommendationType(Enum):
    HOT_PROBLEM = 1
    CF = 2
    CF_TIME = 4
    TWO_TOWER = 5


@cache_wrapper(prefix="cf_rec", timeout=3600)
def _cached_user_recommendations(model_name, user_id, problem_ids, limit):
    return VectorStore(model_name).user_recommendations(user_id, problem_ids, limit)


def get_user_recommended_problems(
    user_id,
    problem_ids,
    recommendation_types,
    limits,
    shuffle=False,
):
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
        if rec_type == RecommendationType.CF:
            return _cached_user_recommendations(
                "collab_filter", user_id, problem_ids, limit
            )
        if rec_type == RecommendationType.CF_TIME:
            return _cached_user_recommendations(
                "collab_filter_time", user_id, problem_ids, limit
            )
        if rec_type == RecommendationType.TWO_TOWER:
            return _cached_user_recommendations(
                "two_tower", user_id, problem_ids, limit
            )
        return []

    all_problems = []
    for rec_type, limit in zip(recommendation_types, limits):
        all_problems += get_problem_ids_from_type(rec_type, limit)

    # deduplicate, preserving scores where available
    seen = set()
    deduped = []
    for obj in all_problems:
        if type(obj) == tuple:
            score, pid = obj
        else:
            score, pid = 0.0, obj
        if pid not in seen:
            deduped.append((score, pid))
            seen.add(pid)

    if shuffle and deduped:
        # Weighted shuffle: higher-scored items more likely near top
        seed = datetime.now().strftime("%d%m%Y")
        rng = random.Random(seed)
        result = []
        remaining = list(deduped)
        while remaining:
            weights = [max(s, 0.01) ** 3 for s, _ in remaining]
            total = sum(weights)
            r = rng.random() * total
            cumulative = 0
            for i, (s, pid) in enumerate(remaining):
                cumulative += weights[i]
                if cumulative >= r:
                    result.append(pid)
                    remaining.pop(i)
                    break
        return result

    return [pid for _, pid in deduped]
