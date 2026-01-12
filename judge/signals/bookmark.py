from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from judge.models import BlogPost, Contest, Problem
from judge.models.bookmark import BookMark, _get_or_create_bookmark
from judge.models.problem import Solution


def invalidate_bookmark_cache_for_object(instance):
    """
    Invalidate bookmark caches before a bookmarkable object is deleted.

    This runs in pre_delete because:
    1. GenericRelation causes Django to CASCADE delete BookMark before post_delete
    2. We need to capture profile IDs before the M2M relationships are cleared
    """
    content_type = ContentType.objects.get_for_model(instance)
    bookmark = BookMark.objects.filter(
        content_type=content_type, object_id=instance.pk
    ).first()

    if bookmark:
        # Fetch profile IDs before deletion - critical to do this BEFORE any delete
        profile_ids = list(bookmark.users.values_list("id", flat=True))

        # Invalidate the cache for the bookmark lookup
        _get_or_create_bookmark.dirty(content_type, instance.pk)

        # Invalidate cache for all users who bookmarked this object
        for profile_id in profile_ids:
            cache_key = f"gaboi:{profile_id}"
            cache.delete(cache_key)


@receiver(pre_delete, sender=Problem)
def problem_bookmark_cleanup(sender, instance, **kwargs):
    invalidate_bookmark_cache_for_object(instance)


@receiver(pre_delete, sender=BlogPost)
def blogpost_bookmark_cleanup(sender, instance, **kwargs):
    invalidate_bookmark_cache_for_object(instance)


@receiver(pre_delete, sender=Contest)
def contest_bookmark_cleanup(sender, instance, **kwargs):
    invalidate_bookmark_cache_for_object(instance)


@receiver(pre_delete, sender=Solution)
def solution_bookmark_cleanup(sender, instance, **kwargs):
    invalidate_bookmark_cache_for_object(instance)
