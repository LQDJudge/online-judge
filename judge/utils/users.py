from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext as _, gettext_lazy

from judge.caching import cache_wrapper
from judge.models import Profile, Rating


@cache_wrapper(prefix="gcr2")
def get_contest_ratings(profile_id):
    ratings = (
        Rating.objects.filter(user_id=profile_id)
        .order_by("-contest__end_time")
        .select_related("contest")
        .defer("contest__description")
    )

    return [
        {
            "rating": rating.rating,
            "rank": rating.rank,
            "contest_name": rating.contest.name,
            "contest_key": rating.contest.key,
            "contest_end_time": rating.contest.end_time,
        }
        for rating in ratings
    ]


def get_awards(profile):
    ratings = get_contest_ratings(profile.id)

    sorted_ratings = sorted(
        ratings, key=lambda x: (x["rank"], -x["contest_end_time"].timestamp())
    )

    result = [
        {
            "label": rating["contest_name"],
            "ranking": rating["rank"],
            "link": reverse("contest_ranking", args=(rating["contest_key"],))
            + "#!"
            + profile.username,
            "date": date_format(rating["contest_end_time"], _("M j, Y")),
        }
        for rating in sorted_ratings
        if rating["rank"] <= 3
    ]

    return result


def get_user_rating_stats(profile_id):
    """
    Get a user's rating statistics.

    Args:
        profile_id: The ID of the user profile

    Returns:
        A dictionary with keys 'min_rating', 'max_rating', and 'contests' (count)
    """
    ratings = get_contest_ratings(profile_id)

    if not ratings:
        return {"min_rating": None, "max_rating": None, "rating_count": 0}

    rating_values = [r["rating"] for r in ratings]

    return {
        "min_rating": min(rating_values),
        "max_rating": max(rating_values),
        "rating_count": len(rating_values),
    }
