"""Keep file identities current for ORM writes, including admin/import paths."""

from django.db.models import F
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from judge.models import (
    Problem,
    ProblemData,
    ProblemSignatureGrader,
    ProblemTestCase,
)
from judge.auto_review.execution_freshness import (
    JUDGING_FIELDS,
    TEST_FIELDS,
    VALIDATOR_FIELDS,
    bump_package_revisions,
    bump_review_files,
)


@receiver(pre_save, sender=ProblemData)
def update_package_file_revisions(
    sender, instance, raw=False, update_fields=None, **kwargs
):
    if raw or not instance.pk:
        return
    groups = {
        "test": TEST_FIELDS,
        "validator": VALIDATOR_FIELDS,
        "judging": JUDGING_FIELDS,
    }
    tracked = set().union(*groups.values())
    if update_fields is not None:
        tracked &= set(update_fields)
    if not tracked:
        return
    old = sender.objects.filter(pk=instance.pk).values(*tracked).first()
    if old is None:
        return
    # No file reads: an uncommitted FieldFile means a new upload, even at the
    # same path. Scalar settings are cheap to compare for non-editor ORM saves.
    for group, fields in groups.items():
        if any(
            (
                (getattr(instance, name).name or "") != (old[name] or "")
                if hasattr(getattr(instance, name), "storage")
                else getattr(instance, name) != old[name]
            )
            or getattr(getattr(instance, name), "_committed", True) is False
            for name in fields & tracked
        ):
            field = group + "_revision"
            # Update directly: narrow update_fields must not drop this increment.
            sender.objects.filter(pk=instance.pk).update(**{field: F(field) + 1})
            setattr(
                instance,
                field,
                sender.objects.values_list(field, flat=True).get(pk=instance.pk),
            )


@receiver(post_save, sender=ProblemTestCase)
@receiver(post_delete, sender=ProblemTestCase)
def update_test_revision(sender, instance, raw=False, **kwargs):
    if not raw:
        bump_package_revisions(instance.dataset_id, {"test"})


@receiver(post_save, sender=ProblemSignatureGrader)
@receiver(post_delete, sender=ProblemSignatureGrader)
def update_grader_revision(sender, instance, raw=False, **kwargs):
    if not raw:
        bump_package_revisions(instance.problem_id, {"judging"})


@receiver(pre_save, sender=Problem)
def update_problem_file_revision(
    sender, instance, raw=False, update_fields=None, **kwargs
):
    if (
        raw
        or not instance.pk
        or (update_fields is not None and "pdf_description" not in update_fields)
    ):
        return
    if getattr(instance.pdf_description, "_committed", True) is False:
        bump_review_files(instance.pk)
        instance.review_files_revision = sender.objects.values_list(
            "review_files_revision", flat=True
        ).get(pk=instance.pk)
