from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext as _, gettext_lazy

from judge.caching import cache_wrapper
from judge.models import Profile


@cache_wrapper(prefix="grr")
def get_rating_rank(profile):
    if profile.is_unlisted:
        return None
    rank = None
    if profile.rating:
        rank = (
            Profile.objects.filter(
                is_unlisted=False,
                rating__gt=profile.rating,
            ).count()
            + 1
        )
    return rank


@cache_wrapper(prefix="gpr")
def get_points_rank(profile):
    if profile.is_unlisted:
        return None
    return (
        Profile.objects.filter(
            is_unlisted=False,
            performance_points__gt=profile.performance_points,
        ).count()
        + 1
    )


@cache_wrapper(prefix="gcr")
def get_contest_ratings(profile):
    return (
        profile.ratings.order_by("-contest__end_time")
        .select_related("contest")
        .defer("contest__description")
    )


def get_awards(profile):
    ratings = get_contest_ratings(profile)

    sorted_ratings = sorted(
        ratings, key=lambda x: (x.rank, -x.contest.end_time.timestamp())
    )

    result = [
        {
            "label": rating.contest.name,
            "ranking": rating.rank,
            "link": reverse("contest_ranking", args=(rating.contest.key,))
            + "#!"
            + profile.username,
            "date": date_format(rating.contest.end_time, _("M j, Y")),
        }
        for rating in sorted_ratings
        if rating.rank <= 3
    ]

    return result
