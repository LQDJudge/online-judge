import errno
import os

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.db.models.signals import post_delete, post_save, m2m_changed
from django.dispatch import receiver

from judge import template_context
from judge.utils.problems import finished_submission
from judge.utils.users import get_points_rank, get_rating_rank
from .models import (
    BlogPost,
    Comment,
    Contest,
    ContestSubmission,
    Judge,
    Language,
    License,
    MiscConfig,
    Organization,
    Problem,
    Profile,
    Submission,
    NavigationBar,
    Solution,
    ContestProblem,
    CourseContest,
)
from judge.models.problem import _get_allowed_languages


def get_pdf_path(basename):
    return os.path.join(settings.DMOJ_PDF_PROBLEM_CACHE, basename)


def unlink_if_exists(file):
    try:
        os.unlink(file)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise


@receiver(post_save, sender=Problem)
def problem_update(sender, instance, **kwargs):
    if hasattr(instance, "_updating_stats_only"):
        return

    cache.delete_many(
        [
            make_template_fragment_key("submission_problem", (instance.id,)),
            "problem_tls:%s" % instance.id,
            "problem_mls:%s" % instance.id,
        ]
    )
    cache.delete_many(
        [
            "generated-meta-problem:%s:%d" % (lang, instance.id)
            for lang, _ in settings.LANGUAGES
        ]
    )

    for lang, _ in settings.LANGUAGES:
        unlink_if_exists(get_pdf_path("%s.%s.pdf" % (instance.code, lang)))


@receiver(post_save, sender=Profile)
def profile_update(sender, instance, **kwargs):
    get_points_rank.dirty(instance.id)
    get_rating_rank.dirty(instance.id)
    if hasattr(instance, "_updating_stats_only"):
        return

    cache.delete_many(
        [
            make_template_fragment_key("org_member_count", (org_id,))
            for org_id in instance.organizations.values_list("id", flat=True)
        ]
    )


@receiver(post_save, sender=Contest)
def contest_update(sender, instance, **kwargs):
    if hasattr(instance, "_updating_stats_only"):
        return

    cache.delete_many(["generated-meta-contest:%d" % instance.id])


@receiver(post_delete, sender=Submission)
def submission_delete(sender, instance, **kwargs):
    finished_submission(instance)
    instance.user.calculate_points()


@receiver(post_delete, sender=ContestSubmission)
def contest_submission_delete(sender, instance, **kwargs):
    participation = instance.participation
    participation.recompute_results()


_misc_config_i18n = [code for code, _ in settings.LANGUAGES]
_misc_config_i18n.append("")


@receiver(post_save, sender=MiscConfig)
def misc_config_update(sender, instance, **kwargs):
    cache.delete_many(
        [
            "misc_config:%s:%s:%s" % (domain, lang, instance.key.split(".")[0])
            for lang in _misc_config_i18n
            for domain in Site.objects.values_list("domain", flat=True)
        ]
    )


@receiver(post_save, sender=ContestSubmission)
def contest_submission_update(sender, instance, **kwargs):
    Submission.objects.filter(id=instance.submission_id).update(
        contest_object_id=instance.participation.contest_id
    )


@receiver(post_save, sender=NavigationBar)
def navbar_update(sender, instance, **kwargs):
    template_context._nav_bar.dirty()


@receiver(post_save, sender=Solution)
def solution_update(sender, instance, **kwargs):
    cache.delete(make_template_fragment_key("solution_content", (instance.id,)))


@receiver(post_delete, sender=ContestProblem)
def contest_problem_delete(sender, instance, **kwargs):
    Submission.objects.filter(
        contest_object=instance.contest, contest__isnull=True
    ).update(contest_object=None)


@receiver(post_delete, sender=CourseContest)
def course_contest_delete(sender, instance, **kwargs):
    instance.contest.delete()


@receiver(m2m_changed, sender=Problem.allowed_languages.through)
def update_allowed_languages(sender, instance, **kwargs):
    if kwargs["action"] in ["post_add", "post_remove", "post_clear"]:
        _get_allowed_languages.dirty((instance.id,))


@receiver(m2m_changed, sender=Problem.authors.through)
def update_problem_authors(sender, instance, **kwargs):
    if kwargs["action"] in ["post_add", "post_remove", "post_clear"]:
        Problem.get_author_ids.dirty(instance)
