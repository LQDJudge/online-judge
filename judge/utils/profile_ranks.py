from django.db.models import Count, Q


def build_profile_rank_map(
    queryset, profiles, order, allowed_sorts, combine_counts=False
):
    profiles = list(profiles)
    if not profiles:
        return {}

    field = order.lstrip("-")
    if field not in allowed_sorts:
        return {profile.id: rank for rank, profile in enumerate(profiles, start=1)}

    desc = order.startswith("-")
    base_queryset = queryset.order_by()
    conditions_by_value = {}

    for profile in profiles:
        value = getattr(profile, field)
        if value in conditions_by_value:
            continue

        if value is None:
            condition = Q(**{f"{field}__isnull": False}) if desc else Q(pk__isnull=True)
        elif desc:
            condition = Q(**{f"{field}__gt": value})
        else:
            condition = Q(**{f"{field}__lt": value}) | Q(**{f"{field}__isnull": True})

        conditions_by_value[value] = condition

    if combine_counts:
        aliases_by_value = {
            value: f"better_{index}" for index, value in enumerate(conditions_by_value)
        }
        better_counts = base_queryset.aggregate(
            **{
                aliases_by_value[value]: Count("pk", filter=condition)
                for value, condition in conditions_by_value.items()
            }
        )
        ranks_by_value = {
            value: better_counts[alias] + 1 for value, alias in aliases_by_value.items()
        }
    else:
        # Small pages over the global Profile table are substantially faster with
        # independent indexed range counts than with a CASE aggregate that scans
        # every visible profile once per displayed value.
        ranks_by_value = {
            value: base_queryset.filter(condition).count() + 1
            for value, condition in conditions_by_value.items()
        }

    return {profile.id: ranks_by_value[getattr(profile, field)] for profile in profiles}
