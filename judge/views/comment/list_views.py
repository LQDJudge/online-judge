from django.conf import settings
from django.db.models import F
from django.http import HttpResponseBadRequest, HttpResponseNotFound
from django.views.generic import ListView

from judge.models import Comment
from judge.models.comment import (
    get_top_level_comment_ids,
    get_reply_ids,
    get_content_author_ids,
)
from judge.views.comment.mixins import is_comment_locked
from judge.views.comment.utils import (
    DEFAULT_COMMENT_LIMIT,
    COMPACT_COMMENT_LIMIT,
    parse_sort_params,
    parse_comment_params,
    get_highlighted_root_tree,
    CommentParams,
)


class CommentListView(ListView):
    template_name = "comments/content-list.html"
    context_object_name = "comment_list"
    limit = DEFAULT_COMMENT_LIMIT
    error_response = None

    def get_comment_root_id(self):
        raise NotImplementedError

    def get_target_comment_id(self):
        return -1

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.sort_by, self.sort_order = parse_sort_params(request)
        self.compact = request.GET.get("compact") == "1"
        if self.compact:
            self.limit = COMPACT_COMMENT_LIMIT
            self.template_name = "comments/inline-comments.html"
        try:
            self.offset = int(request.GET.get("offset", 0))
        except ValueError:
            self.offset = 0

    def get(self, request, *args, **kwargs):
        self.object_list = self.get_queryset()
        if self.error_response:
            return self.error_response
        context = self.get_context_data()
        return self.render_to_response(context)

    def get_queryset(self):
        raise NotImplementedError

    def post_process_comments(self, comments_list, total_comments):
        return comments_list, total_comments

    def get_context_data(self, **kwargs):
        # object_list is now a list from get_queryset()
        comments_list = self.object_list
        total_comments = getattr(self, "total_comments", len(comments_list))

        comments_list, total_comments = self.post_process_comments(
            comments_list, total_comments
        )
        self.total_comments = total_comments

        next_page_offset = self.offset + min(len(comments_list), self.limit)

        # Determine if user is new (can't comment)
        is_new_user = False
        if self.request.user.is_authenticated:
            is_new_user = (
                not self.request.user.is_staff
                and not self.request.profile.submission_set.filter(
                    points=F("problem__points")
                ).exists()
            )

        context = super().get_context_data(**kwargs)
        context.update(
            {
                "profile": self.request.profile,
                "comment_root_id": self.get_comment_root_id(),
                "comment_list": comments_list,
                "vote_hide_threshold": settings.DMOJ_COMMENT_VOTE_HIDE_THRESHOLD,
                "offset": next_page_offset,
                "limit": self.limit,
                "comment_count": total_comments,
                "target_comment": self.get_target_comment_id(),
                "comment_more": total_comments - next_page_offset,
                "sort_by": self.sort_by,
                "sort_order": self.sort_order,
                "compact": getattr(self, "compact", False),
                "comment_lock": is_comment_locked(self.request),
                "is_new_user": is_new_user,
            }
        )
        return context


class TopLevelCommentsView(CommentListView):
    def get_queryset(self):
        params, error = parse_comment_params(self.request)
        if error:
            self.error_response = error
            return []

        self.params = params
        self.offset = params.offset
        self.sort_by = params.sort_by
        self.sort_order = params.sort_order

        if params.content_type_id is None or params.object_id is None:
            self.error_response = HttpResponseBadRequest(
                "Missing content_type_id or object_id"
            )
            return []

        self.comment_root_id = 0

        # Get cached comment IDs (already sorted)
        cached_ids = get_top_level_comment_ids(
            params.content_type_id, params.object_id, self.sort_by, self.sort_order
        )
        self.total_comments = len(cached_ids)

        # Slice IDs for current page
        page_ids = cached_ids[self.offset : self.offset + self.limit]

        if not page_ids:
            return []

        # Fetch comments from cache (no DB query for comment data)
        # get_cached_instances prefills reply count and author profile caches
        comments_list = Comment.get_cached_instances(*page_ids)

        # Prefill vote cache and set vote_score attribute
        profile_id = None
        if self.request.user.is_authenticated:
            profile_id = self.request.profile.id
            Comment.prefill_vote_cache(profile_id, page_ids)

        for comment in comments_list:
            comment.vote_score = comment.get_vote_score(profile_id)

        # Preserve the cached order
        id_to_order = {cid: i for i, cid in enumerate(page_ids)}
        comments_list = sorted(comments_list, key=lambda c: id_to_order.get(c.id, 0))

        return comments_list

    def get_comment_root_id(self):
        return getattr(self, "comment_root_id", 0)

    def get_target_comment_id(self):
        return getattr(
            self, "params", CommentParams("time", "desc", 0, -1, None, None)
        ).target_comment_id

    def get_context_data(self, **kwargs):
        comments_list = self.object_list
        comments_list, self.total_comments = self.post_process_comments(
            comments_list, getattr(self, "total_comments", 0)
        )

        next_page_offset = self.offset + min(len(comments_list), self.limit)

        # Determine if user is new (can't comment)
        is_new_user = False
        if self.request.user.is_authenticated:
            is_new_user = (
                not self.request.user.is_staff
                and not self.request.profile.submission_set.filter(
                    points=F("problem__points")
                ).exists()
            )

        context = super(CommentListView, self).get_context_data(**kwargs)
        context.update(
            {
                "profile": self.request.profile,
                "comment_root_id": self.get_comment_root_id(),
                "comment_list": comments_list,
                "vote_hide_threshold": settings.DMOJ_COMMENT_VOTE_HIDE_THRESHOLD,
                "offset": next_page_offset,
                "limit": self.limit,
                "comment_count": self.total_comments,
                "target_comment": self.get_target_comment_id(),
                "comment_more": self.total_comments - next_page_offset,
                "sort_by": self.sort_by,
                "sort_order": self.sort_order,
                "compact": getattr(self, "compact", False),
                "comment_lock": is_comment_locked(self.request),
                "is_new_user": is_new_user,
            }
        )

        if hasattr(self, "params"):
            context["content_type_id"] = self.params.content_type_id
            context["object_id"] = self.params.object_id
            context["can_hide_comments"] = self._can_hide_comments()

        # Pass highlighted path for nested rendering (excluding root which is in comment_list)
        highlighted_path = getattr(self, "highlighted_path", [])
        if len(highlighted_path) > 1:
            context["highlighted_path"] = highlighted_path[1:]  # Skip root
            context["highlighted_root_id"] = highlighted_path[0].id
            # IDs of ancestors whose replies are shown inline (hide "View N replies" for these)
            context["highlighted_ancestor_ids"] = {c.id for c in highlighted_path[:-1]}
        else:
            context["highlighted_path"] = []
            context["highlighted_root_id"] = None
            context["highlighted_ancestor_ids"] = set()
        return context

    def _can_hide_comments(self):
        """Check if user can hide comments on this content."""
        if not self.request.user.is_authenticated:
            return False
        if self.request.user.has_perm("judge.change_comment"):
            return True
        profile = self.request.profile
        if not profile or not hasattr(self, "params"):
            return False
        # Use cached author IDs
        author_ids = get_content_author_ids(
            self.params.content_type_id, self.params.object_id
        )
        return profile.id in author_ids

    def post_process_comments(self, comments_list, total_comments):
        self.highlighted_path = []
        if (
            self.params.target_comment_id > 0
            and self.offset == 0
            and self.params.content_type_id is not None
        ):
            path = get_highlighted_root_tree(
                self.params.target_comment_id,
                self.params.content_type_id,
                self.params.object_id,
                self.request.user,
            )
            if path:
                # path is [root, child1, ..., target]
                # Only prepend root to comment_list, store full path for template
                root = path[0]
                self.highlighted_path = path  # Full path for nested rendering
                comments_list = [c for c in comments_list if c.id != root.id]
                comments_list = [root] + comments_list
                total_comments += 1
        return comments_list, total_comments


class RepliesView(CommentListView):
    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.sort_by, self.sort_order = parse_sort_params(request, default_order="asc")

    def get_queryset(self):
        try:
            self.comment_id = int(self.request.GET.get("id", 0))
        except ValueError:
            self.error_response = HttpResponseBadRequest()
            return []

        if not self.comment_id:
            self.error_response = HttpResponseBadRequest("Missing comment id")
            return []

        # Verify parent comment exists using cache
        parent_instances = Comment.get_cached_instances(self.comment_id)
        if not parent_instances:
            self.error_response = HttpResponseNotFound()
            return []

        # Get cached reply IDs
        cached_ids = get_reply_ids(self.comment_id, self.sort_order)
        self.total_comments = len(cached_ids)

        # Slice IDs for current page
        page_ids = cached_ids[self.offset : self.offset + self.limit]

        if not page_ids:
            return []

        # Fetch comments from cache
        # get_cached_instances prefills reply count and author profile caches
        comments_list = Comment.get_cached_instances(*page_ids)

        # Prefill vote cache and set vote_score attribute
        profile_id = None
        if self.request.user.is_authenticated:
            profile_id = self.request.profile.id
            Comment.prefill_vote_cache(profile_id, page_ids)

        for comment in comments_list:
            comment.vote_score = comment.get_vote_score(profile_id)

        # Preserve the cached order
        id_to_order = {cid: i for i, cid in enumerate(page_ids)}
        comments_list = sorted(comments_list, key=lambda c: id_to_order.get(c.id, 0))

        return comments_list

    def get_comment_root_id(self):
        return getattr(self, "comment_id", 0)
