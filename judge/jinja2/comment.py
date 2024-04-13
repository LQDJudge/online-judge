from . import registry

from django.contrib.contenttypes.models import ContentType

from judge.models.comment import get_visible_comment_count
from judge.caching import cache_wrapper


@registry.function
def comment_count(obj):
    content_type = ContentType.objects.get_for_model(obj)
    return get_visible_comment_count(content_type, obj.pk)
