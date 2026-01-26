from dataclasses import dataclass
from typing import Optional

from django.db.models import Count, F, FilteredRelation, Q
from django.db.models.expressions import Value
from django.db.models.functions import Coalesce
from django.http import HttpResponseBadRequest
from django.utils.datastructures import MultiValueDictKeyError

from judge.models import Comment
from judge.models.notification import Notification, NotificationCategory
from judge.jinja2.reference import get_user_from_text


DEFAULT_COMMENT_LIMIT = 10
COMPACT_COMMENT_LIMIT = 3


def annotate_comments_for_display(queryset, user):
    """
    Apply standard display annotations to a comment queryset.
    Adds: count_replies, vote_score (if authenticated)
    """
    queryset = queryset.annotate(
        count_replies=Count("replies", distinct=True, filter=Q(replies__hidden=False)),
    )
    if user.is_authenticated:
        queryset = queryset.annotate(
            my_vote=FilteredRelation(
                "votes", condition=Q(votes__voter_id=user.profile.id)
            ),
        ).annotate(vote_score=Coalesce(F("my_vote__score"), Value(0)))
    return queryset


def parse_sort_params(request, default_order="desc"):
    sort_by = request.GET.get("sort_by", "time")
    if sort_by not in ["time", "score"]:
        sort_by = "time"

    sort_order = request.GET.get("sort_order", default_order)
    if sort_order not in ["asc", "desc"]:
        sort_order = default_order

    return sort_by, sort_order


@dataclass
class CommentParams:
    sort_by: str
    sort_order: str
    offset: int
    target_comment_id: int
    content_type_id: Optional[int]
    object_id: Optional[int]


def parse_comment_params(request):
    try:
        sort_by, sort_order = parse_sort_params(request)
        offset = int(request.GET.get("offset", 0))
        target_comment_id = int(request.GET.get("target_comment", -1))

        content_type_id = request.GET.get("content_type_id")
        if content_type_id is not None:
            content_type_id = int(content_type_id)

        object_id = request.GET.get("object_id")
        if object_id is not None:
            object_id = int(object_id)

        return (
            CommentParams(
                sort_by=sort_by,
                sort_order=sort_order,
                offset=offset,
                target_comment_id=target_comment_id,
                content_type_id=content_type_id,
                object_id=object_id,
            ),
            None,
        )
    except (ValueError, MultiValueDictKeyError):
        return None, HttpResponseBadRequest()


def apply_sorting(queryset, sort_by, sort_order):
    if sort_by == "score":
        if sort_order == "desc":
            return queryset.order_by("-score", "-time")
        return queryset.order_by("score", "-time")
    if sort_order == "desc":
        return queryset.order_by("-time")
    return queryset.order_by("time")


def get_highlighted_root_tree(target_comment_id, content_type_id, object_id, user):
    """
    Get the path from root to target comment for highlighting.
    Returns list of cached Comment instances: [root, child, ..., target]
    or None if invalid.
    """
    try:
        target_comment = Comment.objects.get(id=target_comment_id, hidden=False)
        root_comment = target_comment.get_root()

        if not (
            root_comment.content_type_id == content_type_id
            and root_comment.object_id == object_id
        ):
            return None

        if root_comment.hidden:
            return None

        # Get ancestor path from root to target (inclusive)
        # get_ancestors returns ancestors ordered from root to immediate parent
        ancestor_ids = list(
            target_comment.get_ancestors(include_self=True)
            .filter(hidden=False)
            .values_list("id", flat=True)
        )

        if not ancestor_ids:
            return None

        # Fetch cached instances
        comments_list = Comment.get_cached_instances(*ancestor_ids)

        if not comments_list:
            return None

        # Prefill vote cache and set vote_score attribute
        profile_id = None
        if user.is_authenticated:
            profile_id = user.profile.id
            Comment.prefill_vote_cache(profile_id, ancestor_ids)

        for comment in comments_list:
            comment.vote_score = comment.get_vote_score(profile_id) if profile_id else 0

        # Preserve ancestor order (root first, target last)
        id_to_order = {cid: i for i, cid in enumerate(ancestor_ids)}
        comments_list = sorted(comments_list, key=lambda c: id_to_order.get(c.id, 0))

        return comments_list
    except (Comment.DoesNotExist, ValueError):
        return None


def get_html_link_notification(comment):
    return f'<a href="{comment.get_absolute_url()}">{comment.page_title}</a>'


def add_mention_notifications(comment):
    """Create notifications for users mentioned in comment."""
    users_mentioned = (
        get_user_from_text(comment.body)
        .exclude(id=comment.author.id)
        .values_list("id", flat=True)
    )
    link = get_html_link_notification(comment)
    Notification.objects.bulk_create_notifications(
        user_ids=list(users_mentioned),
        category=NotificationCategory.MENTION,
        html_link=link,
        author=comment.author,
    )
