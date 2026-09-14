from django.contrib.auth.models import User
from django.db import transaction
from django.db.models.signals import post_save, m2m_changed
from django.dispatch import receiver

from judge.models import Profile, Organization
from judge.models.profile import _get_profile, get_profile_public_identity


@receiver(post_save, sender=User)
def on_user_save(sender, instance, **kwargs):
    try:
        profile = instance.profile
        _get_profile.dirty(profile.id)
        get_profile_public_identity.dirty(profile.id)
    except:
        pass


@receiver(m2m_changed, sender=Profile.organizations.through)
def on_profile_organization_change(sender, instance, action, **kwargs):
    forward = isinstance(instance, Profile)
    if action == "pre_clear":
        manager = instance.organizations if forward else instance.members
        instance._school_cache_clear_ids = list(manager.values_list("pk", flat=True))
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    ids = kwargs.get("pk_set") or getattr(instance, "_school_cache_clear_ids", [])
    profiles, orgs = ([instance.pk], ids) if forward else (ids, [instance.pk])

    def invalidate():
        Profile.get_organization_ids.dirty_multi([(pk,) for pk in profiles])
        Organization.get_member_ids.dirty_multi([(pk,) for pk in orgs])

    invalidate()
    transaction.on_commit(invalidate)
    if action == "post_clear":
        instance._school_cache_clear_ids = []


@receiver(m2m_changed, sender=Organization.admins.through)
@receiver(m2m_changed, sender=Organization.moderators.through)
def on_organization_admin_change(sender, instance, action, **kwargs):
    forward = isinstance(instance, Organization)
    is_admin_relation = sender is Organization.admins.through
    if action == "pre_clear":
        if is_admin_relation:
            manager = instance.admins if forward else instance.admin_of
        else:
            manager = (
                instance.moderators if forward else instance.moderated_organizations
            )
        instance._school_admin_clear_ids = list(manager.values_list("pk", flat=True))
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    ids = kwargs.get("pk_set") or getattr(instance, "_school_admin_clear_ids", [])
    orgs, profiles = ([instance.pk], ids) if forward else (ids, [instance.pk])

    def invalidate():
        cache_method = (
            Organization.get_admin_ids
            if is_admin_relation
            else Organization.get_moderator_ids
        )
        cache_method.dirty_multi([(pk,) for pk in orgs])
        if is_admin_relation:
            Profile.get_admin_organization_ids.dirty_multi([(pk,) for pk in profiles])

    invalidate()
    transaction.on_commit(invalidate)
    if action == "post_clear":
        instance._school_admin_clear_ids = []
