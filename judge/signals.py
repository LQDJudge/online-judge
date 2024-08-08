import errno
import os

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

import judge
from judge.utils.problems import finished_submission
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
)


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
            make_template_fragment_key("problem_html", (instance.id, lang))
            for lang, _ in settings.LANGUAGES
        ]
    )
    cache.delete_many(
        [
            "generated-meta-problem:%s:%d" % (lang, instance.id)
            for lang, _ in settings.LANGUAGES
        ]
    )
    Problem.get_authors.dirty(instance)

    for lang, _ in settings.LANGUAGES:
        unlink_if_exists(get_pdf_path("%s.%s.pdf" % (instance.code, lang)))


@receiver(post_save, sender=Profile)
def profile_update(sender, instance, **kwargs):
    judge.utils.users.get_points_rank.dirty(instance.id)
    judge.utils.users.get_rating_rank.dirty(instance.id)
    if hasattr(instance, "_updating_stats_only"):
        return

    cache.delete_many(
        [make_template_fragment_key("user_about", (instance.id,))]
        + [
            make_template_fragment_key("org_member_count", (org_id,))
            for org_id in instance.organizations.values_list("id", flat=True)
        ]
    )

    judge.models.profile._get_basic_info.dirty(instance.id)


@receiver(post_save, sender=Contest)
def contest_update(sender, instance, **kwargs):
    if hasattr(instance, "_updating_stats_only"):
        return

    cache.delete_many(
        ["generated-meta-contest:%d" % instance.id]
        + [make_template_fragment_key("contest_html", (instance.id,))]
    )


@receiver(post_save, sender=License)
def license_update(sender, instance, **kwargs):
    cache.delete(make_template_fragment_key("license_html", (instance.id,)))


@receiver(post_save, sender=Language)
def language_update(sender, instance, **kwargs):
    cache.delete_many(
        [make_template_fragment_key("language_html", (instance.id,)), "lang:cn_map"]
    )


@receiver(post_save, sender=Judge)
def judge_update(sender, instance, **kwargs):
    cache.delete(make_template_fragment_key("judge_html", (instance.id,)))


@receiver(post_save, sender=Comment)
def comment_update(sender, instance, **kwargs):
    cache.delete("comment_feed:%d" % instance.id)


@receiver(post_save, sender=BlogPost)
def post_update(sender, instance, **kwargs):
    cache.delete(make_template_fragment_key("post_content", (instance.id,)))
    BlogPost.get_authors.dirty(instance)


@receiver(post_delete, sender=Submission)
def submission_delete(sender, instance, **kwargs):
    finished_submission(instance)
    instance.user.calculate_points()


@receiver(post_delete, sender=ContestSubmission)
def contest_submission_delete(sender, instance, **kwargs):
    participation = instance.participation
    participation.recompute_results()


@receiver(post_save, sender=Organization)
def organization_update(sender, instance, **kwargs):
    cache.delete_many([make_template_fragment_key("organization_html", (instance.id,))])


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
    judge.template_context._nav_bar.dirty()


@receiver(post_save, sender=Solution)
def solution_update(sender, instance, **kwargs):
    cache.delete(make_template_fragment_key("solution_content", (instance.id,)))
