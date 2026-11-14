"""Apply Lock on a real transition to site-wide public visibility."""

from judge.utils.public_visibility import PROBLEM_PUBLIC_SCOPE, CONTEST_PUBLIC_SCOPE


def apply_publication_lock(instance, visibility_field, save_kwargs):
    """Include Lock in the publication write; explicit later unlock stays valid.

    Restricted objects are not site-public merely because their visibility flag
    is enabled. Restriction-only updates can also be the publication transition.
    QuerySet.update must still set Lock explicitly at its authorized write path.
    """
    if instance.is_locked:
        return
    scope = {"problem": PROBLEM_PUBLIC_SCOPE, "contest": CONTEST_PUBLIC_SCOPE}.get(
        instance._meta.model_name
    )
    if scope is None:
        return
    update_fields = save_kwargs.get("update_fields")
    if update_fields is not None and not set(scope).intersection(update_fields):
        return
    previous = (
        type(instance).objects.filter(pk=instance.pk).values(*scope).first()
        if instance.pk
        else None
    )
    proposed = {
        field: (
            getattr(instance, field)
            if update_fields is None or field in update_fields or previous is None
            else previous[field]
        )
        for field in scope
    }
    public = all(proposed[field] == expected for field, expected in scope.items())
    was_public = bool(previous) and all(
        previous[field] == expected for field, expected in scope.items()
    )
    if public and not was_public:
        instance.is_locked = True
        if update_fields is not None:
            save_kwargs["update_fields"] = set(update_fields) | {"is_locked"}
