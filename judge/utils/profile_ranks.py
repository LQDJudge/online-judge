from django.db.models import Q


def build_profile_rank_map(queryset, profiles, order, allowed_sorts):
    profiles = list(profiles)
    if not profiles:
        return {}

    field = order.lstrip("-")
    if field not in allowed_sorts:
        return {profile.id: rank for rank, profile in enumerate(profiles, start=1)}

    desc = order.startswith("-")
    base_queryset = queryset.order_by()
    ranks_by_value = {}

    for profile in profiles:
        value = getattr(profile, field)
        if value in ranks_by_value:
            continue

        if value is None:
            condition = Q(**{f"{field}__isnull": False}) if desc else Q(pk__isnull=True)
        elif desc:
            condition = Q(**{f"{field}__gt": value})
        else:
            condition = Q(**{f"{field}__lt": value}) | Q(**{f"{field}__isnull": True})

        ranks_by_value[value] = base_queryset.filter(condition).count() + 1

    return {profile.id: ranks_by_value[getattr(profile, field)] for profile in profiles}
