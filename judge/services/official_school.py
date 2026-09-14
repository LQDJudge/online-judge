"""School writes: profiles -> organizations -> chat rooms, all in ID order.

Never use cached memberships for authorization or uniqueness. Direct through-table
SQL/bulk_create bypasses Django signals and is not a supported enrollment API.
"""

import hmac
import json
from contextlib import contextmanager

from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext as _

from chat_box.models import Room
from judge.models import Block, OfficialSchool, Organization, Profile
from judge.models.notification import Notification, NotificationCategory
from judge.models.official_school import school_write


@contextmanager
def school_mutation():
    token = school_write.set(True)
    try:
        yield
    finally:
        school_write.reset(token)


def school_for_profile(profile_id):
    return OfficialSchool.objects.filter(organization__member=profile_id).first()


def school_map(profile_ids):
    return dict(
        Organization.objects.filter(
            official_school__isnull=False,
            member__in=profile_ids,
        ).values_list("member", "id")
    )


def can_manage_school(user, organization_id):
    return user.is_authenticated and (
        user.is_superuser
        or Organization.objects.filter(
            pk=organization_id,
            admins__user_id=user.pk,
        ).exists()
    )


def lock_school_scope(profile_ids, organization_ids):
    list(
        Profile.objects.select_for_update()
        .filter(
            pk__in=profile_ids,
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    organization_ids = set(organization_ids) | set(
        Organization.objects.filter(
            official_school__isnull=False,
            member__in=profile_ids,
        ).values_list("pk", flat=True)
    )
    organizations = list(
        Organization.objects.select_for_update()
        .filter(
            pk__in=organization_ids,
        )
        .order_by("pk")
    )
    list(
        Room.objects.select_for_update()
        .filter(
            organization_id__in=organization_ids,
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    return {org.pk: org for org in organizations}


def _log(actor, profile_id, old_id, new_id, action):
    LogEntry.objects.log_action(
        user_id=actor.pk,
        content_type_id=ContentType.objects.get_for_model(Profile).pk,
        object_id=profile_id,
        object_repr=str(profile_id),
        action_flag=CHANGE,
        change_message=json.dumps(
            {
                "official_school": {
                    "action": action,
                    "profile": profile_id,
                    "old_school": old_id,
                    "new_school": new_id,
                }
            }
        ),
    )


def _notify(actor_id, profile_ids, organization_id, teacher_ids=()):
    recipients = sorted(set(profile_ids) | set(teacher_ids))

    def send():
        org = Organization(id=organization_id)
        Notification.objects.bulk_create_notifications(
            user_ids=recipients,
            category=NotificationCategory.ORGANIZATION,
            html_link=format_html(
                '<a href="{}">{}</a>',
                reverse("organization_home", args=[org.id, org.slug]),
                _("Official school membership updated: %(school)s")
                % {"school": org.name},
            ),
            author=Profile(id=actor_id),
            extra_data={"official_school": organization_id},
            deduplicate=False,
        )

    transaction.on_commit(send)


@transaction.atomic
def configure_school(actor, organization, *, is_active=True):
    """Change enrollment availability for an existing school; never convert groups."""
    if not actor.is_superuser:
        raise PermissionDenied
    locked = Organization.objects.select_for_update().get(pk=organization.pk)
    school = OfficialSchool.objects.get(pk=locked.pk)
    with school_mutation():
        school.is_active = is_active
        school.save(update_fields=["is_active"])
    return school


@transaction.atomic
def create_school(actor, *, name, slug, short_name, teachers, is_active=True):
    if not actor.is_superuser:
        raise PermissionDenied
    teachers = list(teachers)
    if not teachers:
        raise ValidationError(_("An official school needs at least one administrator."))
    org = Organization(
        name=name,
        slug=slug,
        short_name=short_name,
        about="",
        registrant=actor.profile,
        is_open=False,
        is_community=False,
    )
    org.full_clean(exclude=["about"])
    org.save()
    org.admins.add(*teachers)
    with school_mutation():
        return OfficialSchool.objects.create(
            organization=org, verified_by=actor.profile, is_active=is_active
        )


@transaction.atomic
def enroll_school(
    actor, organization_id, profile_id, *, code=None, expected_school=None
):
    organizations = lock_school_scope([profile_id], [organization_id])
    org = organizations[organization_id]
    school = OfficialSchool.objects.get(pk=org.pk)
    if not school.is_active:
        raise ValidationError(_("This school's enrollment is disabled."))
    current = school_for_profile(profile_id)
    old_id = current.pk if current else None
    if code is not None:
        if (
            actor.profile.pk != profile_id
            or not code
            or not org.access_code
            or not hmac.compare_digest(
                code.encode("utf-8"), org.access_code.encode("utf-8")
            )
        ):
            raise PermissionDenied
        if old_id != expected_school:
            raise ValidationError(
                _("Your school changed. Open the invitation again to confirm.")
            )
    else:
        if not can_manage_school(actor, org.pk):
            raise PermissionDenied
        if old_id is not None and old_id != org.pk:
            raise ValidationError(
                _("A transfer requires the student's invitation confirmation.")
            )
    if Block.objects.filter(
        blocker_type=ContentType.objects.get_for_model(Profile),
        blocker_id=profile_id,
        blocked_type=ContentType.objects.get_for_model(Organization),
        blocked_id=org.pk,
    ).exists():
        raise ValidationError(_("This student has blocked the school."))
    if old_id == org.pk:
        return False
    if org.slots is not None and org.members.count() >= org.slots:
        raise ValidationError(_("This school has reached its member limit."))
    profile = Profile.objects.get(pk=profile_id)
    with school_mutation():
        if old_id is not None:
            profile.organizations.remove(old_id)
        profile.organizations.add(org)
    _log(actor, profile_id, old_id, org.pk, "transfer" if old_id else "enroll")
    teachers = set(org.admins.values_list("pk", flat=True))
    _notify(actor.profile.pk, [profile_id], org.pk, teachers)
    if old_id:
        # Former-school teachers only need to know that the student left. Do not
        # disclose the destination school through the notification link or text.
        _notify(
            actor.profile.pk,
            [],
            old_id,
            organizations[old_id].admins.values_list("pk", flat=True),
        )
    return True


@transaction.atomic
def remove_school_member(actor, organization_id, profile_id):
    org = lock_school_scope([profile_id], [organization_id])[organization_id]
    if actor.profile.pk != profile_id and not can_manage_school(actor, org.pk):
        raise PermissionDenied
    if not org.members.filter(pk=profile_id).exists():
        return
    with school_mutation():
        org.members.remove(profile_id)
    _log(
        actor,
        profile_id,
        org.pk,
        None,
        "leave" if actor.profile.pk == profile_id else "remove",
    )
    _notify(
        actor.profile.pk, [profile_id], org.pk, org.admins.values_list("pk", flat=True)
    )


@transaction.atomic
def bulk_enroll_school(actor, organization_id, usernames):
    usernames = list(dict.fromkeys(usernames))
    if len(usernames) > 500:
        raise ValidationError(_("Add at most 500 students at a time."))
    if not can_manage_school(actor, organization_id):
        raise PermissionDenied
    found_profiles = list(
        Profile.objects.filter(user__username__in=usernames).select_related("user")
    )
    by_name = {profile.user.username: profile for profile in found_profiles}
    profiles = [by_name[name] for name in usernames if name in by_name]
    org = lock_school_scope([p.pk for p in profiles], [organization_id])[
        organization_id
    ]
    if not can_manage_school(actor, organization_id):
        raise PermissionDenied
    if not OfficialSchool.objects.filter(pk=org.pk, is_active=True).exists():
        raise ValidationError(_("This school's enrollment is disabled."))
    result = {
        "added": [],
        "existing": [],
        "unknown": [],
        "blocked": [],
        "conflict": [],
        "full": [],
    }
    result["unknown"] = [name for name in usernames if name not in by_name]
    affiliations = school_map([p.pk for p in profiles])
    blocked = set(
        Block.objects.filter(
            blocker_type=ContentType.objects.get_for_model(Profile),
            blocker_id__in=[p.pk for p in profiles],
            blocked_type=ContentType.objects.get_for_model(Organization),
            blocked_id=org.pk,
        ).values_list("blocker_id", flat=True)
    )
    capacity = (
        max(0, org.slots - org.members.count())
        if org.slots is not None
        else len(profiles)
    )
    added = []
    for profile in profiles:
        if affiliations.get(profile.pk) == org.pk:
            result["existing"].append(profile.user.username)
        elif profile.pk in blocked:
            result["blocked"].append(profile.user.username)
        elif profile.pk in affiliations:
            result["conflict"].append(profile.user.username)
        elif len(added) >= capacity:
            result["full"].append(profile.user.username)
        else:
            added.append(profile)
            result["added"].append(profile.user.username)
    if added:
        with school_mutation():
            org.members.add(*added)
        content_type = ContentType.objects.get_for_model(Profile)
        LogEntry.objects.bulk_create(
            [
                LogEntry(
                    user_id=actor.pk,
                    content_type=content_type,
                    object_id=p.pk,
                    object_repr=p.user.username,
                    action_flag=CHANGE,
                    change_message=json.dumps(
                        {
                            "official_school": {
                                "action": "enroll",
                                "profile": p.pk,
                                "old_school": None,
                                "new_school": org.pk,
                            }
                        }
                    ),
                )
                for p in added
            ]
        )
        _notify(
            actor.profile.pk,
            [p.pk for p in added],
            org.pk,
            org.admins.values_list("pk", flat=True),
        )
    return result
