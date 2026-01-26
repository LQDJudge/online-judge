"""
Comment views package.

Provides all comment-related views and utilities.
"""

from judge.views.comment.actions import (
    vote_comment,
    upvote_comment,
    downvote_comment,
    comment_hide,
    post_comment,
)
from judge.views.comment.detail_views import (
    CommentContent,
    CommentRevisionAjax,
    CommentVotesAjax,
)
from judge.views.comment.edit_views import CommentEdit, CommentEditAjax
from judge.views.comment.feed import CommentFeed
from judge.views.comment.forms import CommentForm, CommentEditForm
from judge.views.comment.list_views import (
    CommentListView,
    TopLevelCommentsView,
    RepliesView,
)
from judge.views.comment.mixins import (
    CommentMixin,
    CommentableMixin,
    is_comment_locked,
)
from judge.views.comment.utils import (
    annotate_comments_for_display,
    add_mention_notifications,
    parse_sort_params,
    parse_comment_params,
    apply_sorting,
    get_highlighted_root_tree,
    get_html_link_notification,
    CommentParams,
    DEFAULT_COMMENT_LIMIT,
    COMPACT_COMMENT_LIMIT,
)

__all__ = [
    # Actions
    "add_mention_notifications",
    "vote_comment",
    "upvote_comment",
    "downvote_comment",
    "comment_hide",
    "post_comment",
    # Detail views
    "CommentContent",
    "CommentRevisionAjax",
    "CommentVotesAjax",
    # Edit views
    "CommentEdit",
    "CommentEditAjax",
    # Feed
    "CommentFeed",
    # Forms
    "CommentForm",
    "CommentEditForm",
    # List views
    "CommentListView",
    "TopLevelCommentsView",
    "RepliesView",
    # Mixins
    "CommentMixin",
    "CommentableMixin",
    "is_comment_locked",
    # Utils
    "annotate_comments_for_display",
    "parse_sort_params",
    "parse_comment_params",
    "apply_sorting",
    "get_highlighted_root_tree",
    "get_html_link_notification",
    "CommentParams",
    "DEFAULT_COMMENT_LIMIT",
    "COMPACT_COMMENT_LIMIT",
]
