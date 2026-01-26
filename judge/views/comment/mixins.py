from django.contrib.contenttypes.models import ContentType
from django.db.models import F
from django.http import Http404

from judge.models import Comment
from judge.models.comment import (
    get_visible_comment_count,
    get_visible_top_level_comment_count,
)
from judge.views.comment.forms import CommentForm
from judge.views.comment.utils import parse_sort_params


class CommentMixin(object):
    """Mixin for comment-related views."""

    model = Comment
    pk_url_kwarg = "id"
    context_object_name = "comment"


def is_comment_locked(request):
    """
    Check if comments are locked for the current user.

    Returns True if comments are locked, False otherwise.
    """
    if request.user.has_perm("judge.override_comment_lock"):
        return False
    return request.in_contest and request.participation.contest.use_clarifications


class CommentableMixin:
    """
    Mixin for views that include comments.

    Provides the necessary context for AJAX-loaded comments and comment form,
    but does not render comments directly.
    """

    def get_comment_context(self, context=None):
        """
        Add comment-related context data.

        Can be called from get_context_data() in the implementing view.
        """
        if context is None:
            context = {}

        # Get metadata about comments for this object
        content_type = ContentType.objects.get_for_model(self.object)
        object_id = self.object.pk

        total_comment_count = get_visible_comment_count(content_type.id, object_id)
        top_level_count = get_visible_top_level_comment_count(
            content_type.id, object_id
        )

        if self.request.user.is_authenticated:
            context["is_new_user"] = (
                not self.request.user.is_staff
                and not self.request.profile.submission_set.filter(
                    points=F("problem__points")
                ).exists()
            )

        sort_by, sort_order = parse_sort_params(self.request)

        target_comment = -1
        target_comment_id = self.request.GET.get("target_comment")
        if target_comment_id:
            try:
                comment_obj = Comment.objects.get(id=int(target_comment_id))
                if (
                    comment_obj.content_type != content_type
                    or comment_obj.object_id != object_id
                ):
                    raise Http404
                # Pass the original target_comment ID (not root) so the backend
                # can build the full path from root to target
                target_comment = comment_obj.id
            except (ValueError, Comment.DoesNotExist):
                pass

        context.update(
            {
                "comment_lock": is_comment_locked(self.request),
                "has_comments": top_level_count > 0,
                "all_comment_count": total_comment_count,
                "comment_content_type_id": content_type.id,
                "comment_object_id": object_id,
                "sort_by": sort_by,
                "sort_order": sort_order,
                "target_comment": target_comment,
                "comment_form": CommentForm(self.request, initial={"parent": None}),
            }
        )

        return context
