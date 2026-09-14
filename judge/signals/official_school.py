from django.core.exceptions import ValidationError
from django.db.models.signals import m2m_changed, pre_delete, pre_save
from django.dispatch import receiver
from django.utils.translation import gettext as _

from judge.models import OfficialSchool, Organization, Profile
from judge.models.official_school import school_write


@receiver(pre_delete, sender=OfficialSchool)
def protect_school(sender, instance, **kwargs):
    raise ValidationError(_("Archive official schools instead of deleting them."))


@receiver(pre_save, sender=Organization)
def close_school(sender, instance, **kwargs):
    if instance.pk and OfficialSchool.objects.filter(pk=instance.pk).exists():
        if instance.is_open or instance.is_community:
            raise ValidationError(
                _("Official schools must remain closed and non-community.")
            )


@receiver(m2m_changed, sender=Profile.organizations.through)
def guard_school_membership(sender, instance, action, reverse, pk_set, **kwargs):
    if action not in ("pre_add", "pre_remove", "pre_clear"):
        return
    if school_write.get():
        return
    if isinstance(instance, Profile):
        if action == "pre_clear":
            # A clear has no explicit organization IDs. Serialize it with school
            # enrollment, then re-read the affiliations after acquiring the lock.
            Profile.objects.select_for_update().get(pk=instance.pk)
            org_ids = list(instance.organizations.values_list("pk", flat=True))
        else:
            org_ids = list(pk_set)
    else:
        org_ids = [instance.pk]
    if OfficialSchool.objects.filter(pk__in=org_ids).exists():
        raise ValidationError(
            _("Use the school invitation or member-management workflow.")
        )


@receiver(m2m_changed, sender=Organization.admins.through)
def protect_last_school_admin(sender, instance, action, reverse, pk_set, **kwargs):
    if action not in ("pre_add", "pre_remove", "pre_clear"):
        return
    if isinstance(instance, Organization):
        org_ids = [instance.pk]
        removed_ids = (
            list(instance.admins.values_list("pk", flat=True))
            if action == "pre_clear"
            else pk_set
        )
    else:
        org_ids = (
            list(instance.admin_of.values_list("pk", flat=True))
            if action == "pre_clear"
            else pk_set
        )
        removed_ids = [instance.pk]
    schools = (
        Organization.objects.select_for_update()
        .filter(
            pk__in=org_ids,
            official_school__isnull=False,
        )
        .order_by("pk")
    )
    for org in schools:
        if action != "pre_add" and not org.admins.exclude(pk__in=removed_ids).exists():
            raise ValidationError(
                _("An official school needs at least one administrator.")
            )


@receiver(m2m_changed, sender=Organization.moderators.through)
def prevent_school_moderators(sender, instance, action, reverse, pk_set, **kwargs):
    if action != "pre_add" or not pk_set:
        return
    org_ids = pk_set if isinstance(instance, Profile) else [instance.pk]
    if OfficialSchool.objects.filter(pk__in=org_ids).exists():
        raise ValidationError(
            _("Official schools use administrators instead of moderators.")
        )


@receiver(pre_delete, sender=Profile)
def protect_last_teacher_account(sender, instance, **kwargs):
    # Django sends all pre_delete signals before removing M2M rows. Checking only
    # the current admin count therefore allows bulk or concurrent deletions to
    # remove every teacher. Require the role to be removed explicitly through the
    # protected administrator workflow first.
    if Organization.objects.filter(
        official_school__isnull=False, admins=instance
    ).exists():
        raise ValidationError(
            _(
                "Remove this account from official school administration before deleting it."
            )
        )
