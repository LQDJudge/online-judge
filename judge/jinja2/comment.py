from . import registry

from django.contrib.contenttypes.models import ContentType

from judge.models.comment import get_visible_comment_count


@registry.function
def comment_count(obj):
    content_type = ContentType.objects.get_for_model(obj)
    return get_visible_comment_count(content_type.id, obj.pk)


@registry.function
def get_content_type_id(obj):
    """Get the ContentType ID for an object, used for AJAX comment loading."""
    content_type = ContentType.objects.get_for_model(obj)
    return content_type.id
