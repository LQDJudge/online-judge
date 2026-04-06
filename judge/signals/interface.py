from django.conf import settings
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.db.models.signals import post_save, m2m_changed
from django.dispatch import receiver

from judge import template_context
from judge.models import BlogPost, MiscConfig, NavigationBar, Solution
from judge.models.interface import _get_blogpost_organization_ids


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


@receiver(post_save, sender=NavigationBar)
def navbar_update(sender, instance, **kwargs):
    template_context._nav_bar.dirty()


@receiver(post_save, sender=Solution)
def solution_update(sender, instance, **kwargs):
    cache.delete(make_template_fragment_key("solution_content", (instance.id,)))


@receiver(m2m_changed, sender=BlogPost.organizations.through)
def update_blogpost_organizations(sender, instance, action, **kwargs):
    if action in ["post_add", "post_remove", "post_clear"]:
        instance.is_organization_private = instance.organizations.exists()
        instance.save(update_fields=["is_organization_private"])
        _get_blogpost_organization_ids.dirty(instance.id)


@receiver(m2m_changed, sender=BlogPost.authors.through)
def update_blogpost_authors(sender, instance, action, **kwargs):
    if action in ["post_add", "post_remove", "post_clear"]:
        BlogPost.get_author_ids.dirty(instance)
