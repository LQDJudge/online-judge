"""Resolve pasted usernames in one query, preserving input order."""

from django.contrib.contenttypes.models import ContentType

from judge.models import Block, Organization, Profile


def resolve_member_usernames(usernames):
    unique = {}
    for name in usernames:
        unique.setdefault(name.casefold(), name)
    names = list(unique.values())
    found = {
        profile.user.username.casefold(): profile
        for profile in Profile.objects.filter(user__username__in=names).select_related(
            "user"
        )
    }
    profiles = [found[name.casefold()] for name in names if name.casefold() in found]
    unknown = [name for name in names if name.casefold() not in found]
    return profiles, unknown


def organization_member_plan(organization, usernames):
    profiles, unknown = resolve_member_usernames(usernames)
    ids = [profile.pk for profile in profiles]
    existing = set(organization.members.filter(pk__in=ids).values_list("pk", flat=True))
    blocked = set(
        Block.objects.filter(
            blocker_type=ContentType.objects.get_for_model(Profile),
            blocker_id__in=ids,
            blocked_type=ContentType.objects.get_for_model(Organization),
            blocked_id=organization.pk,
        ).values_list("blocker_id", flat=True)
    )
    capacity = (
        max(0, organization.slots - organization.members.count())
        if organization.slots is not None
        else len(profiles)
    )
    result = {
        key: []
        for key in ("added", "existing", "unknown", "blocked", "conflict", "full")
    }
    result["unknown"] = unknown
    additions = []
    for profile in profiles:
        if profile.pk in existing:
            key = "existing"
        elif profile.pk in blocked:
            key = "blocked"
        elif len(additions) >= capacity:
            key = "full"
        else:
            key = "added"
            additions.append(profile)
        result[key].append(profile.user.username)
    return result, additions
