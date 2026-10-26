from django.conf import settings


def apply_publication_lock(instance, visibility_field, save_kwargs):
    """Lock a visibility transition in the same INSERT/UPDATE as publication.

    Deliberately unlocked already-public objects stay unlocked on later saves.
    QuerySet.update remains an explicit write path that must set Lock itself.
    """
    if not getattr(settings, "AUTO_REVIEW_COORDINATOR_ENABLED", False):
        return
    update_fields = save_kwargs.get("update_fields")
    if instance.is_locked or not getattr(instance, visibility_field):
        return
    if update_fields is not None and visibility_field not in update_fields:
        return
    previous_public = (
        type(instance)
        .objects.filter(pk=instance.pk)
        .values_list(visibility_field, flat=True)
        .first()
        if instance.pk
        else False
    )
    if not previous_public:
        instance.is_locked = True
        if update_fields is not None:
            save_kwargs["update_fields"] = set(update_fields) | {"is_locked"}
