from django.contrib.auth.models import User
from django.db.models.signals import post_save, m2m_changed
from django.dispatch import receiver

from judge.models import Profile, Organization
from judge.models.profile import _get_profile


@receiver(post_save, sender=User)
def on_user_save(sender, instance, **kwargs):
    try:
        profile = instance.profile
        _get_profile.dirty(profile.id)
    except:
        pass


@receiver(m2m_changed, sender=Profile.organizations.through)
def on_profile_organization_change(sender, instance, action, **kwargs):
    if action in ["post_add", "post_remove", "post_clear"]:
        if isinstance(instance, Profile):
            Profile.get_organization_ids.dirty(instance)

            # Also invalidate the organization's member cache
            org_pk_set = kwargs.get("pk_set")
            if org_pk_set:
                for org_id in org_pk_set:
                    Organization.get_member_ids.dirty(org_id)


@receiver(m2m_changed, sender=Organization.admins.through)
def on_organization_admin_change(sender, instance, action, **kwargs):
    if action in ["post_add", "post_remove", "post_clear"]:
        if isinstance(instance, Organization):
            Organization.get_admin_ids.dirty(instance)

            # Also invalidate the admin_of cache for each affected profile
            profile_pk_set = kwargs.get("pk_set")
            if profile_pk_set:
                for profile_id in profile_pk_set:
                    Profile.get_admin_organization_ids.dirty(profile_id)
