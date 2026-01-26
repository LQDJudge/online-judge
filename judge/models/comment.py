import itertools

from django.contrib.contenttypes.fields import GenericRelation
from django.db import models
from django.db.models import CASCADE
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from mptt.fields import TreeForeignKey
from mptt.models import MPTTModel
from reversion.models import Version

from judge.models.contest import Contest
from judge.models.interface import BlogPost
from judge.models.problem import Problem, Solution
from judge.models.profile import Profile
from judge.utils.cachedict import CacheDict
from judge.caching import cache_wrapper, CacheableModel


__all__ = [
    "Comment",
    "CommentLock",
    "CommentVote",
    "Notification",
    "get_visible_comment_count",
    "get_visible_top_level_comment_count",
    "get_user_vote_on_comment",
    "get_visible_reply_count",
    "get_top_level_comment_ids",
    "get_reply_ids",
]


class VersionRelation(GenericRelation):
    def __init__(self):
        super(VersionRelation, self).__init__(Version, object_id_field="object_id")

    def get_extra_restriction(self, where_class, alias, remote_alias):
        cond = super(VersionRelation, self).get_extra_restriction(
            where_class, alias, remote_alias
        )
        field = self.remote_field.model._meta.get_field("db")
        lookup = field.get_lookup("exact")(field.get_col(remote_alias), "default")
        cond.add(lookup, "AND")
        return cond


class Comment(CacheableModel, MPTTModel):
    author = models.ForeignKey(Profile, verbose_name=_("commenter"), on_delete=CASCADE)
    time = models.DateTimeField(verbose_name=_("posted time"), auto_now_add=True)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    linked_object = GenericForeignKey("content_type", "object_id")
    score = models.IntegerField(verbose_name=_("votes"), default=0)
    body = models.TextField(verbose_name=_("body of comment"), max_length=8192)
    hidden = models.BooleanField(verbose_name=_("hide the comment"), default=0)
    parent = TreeForeignKey(
        "self",
        verbose_name=_("parent"),
        null=True,
        blank=True,
        related_name="replies",
        on_delete=CASCADE,
    )
    revision_count = models.PositiveIntegerField(default=1)

    versions = VersionRelation()

    class Meta:
        verbose_name = _("comment")
        verbose_name_plural = _("comments")
        indexes = [
            models.Index(fields=["content_type", "object_id", "hidden"]),
        ]

    class MPTTMeta:
        order_insertion_by = ["-time"]

    # CacheableModel implementation
    @classmethod
    def get_cached_dict(cls, comment_id):
        return _get_comment(comment_id)

    @classmethod
    def dirty_cache(cls, *ids):
        id_list = [(id,) for id in ids]
        _get_comment.dirty_multi(id_list)

    @classmethod
    def get_cached_instances(cls, *ids):
        """
        Batch retrieve Comment instances with cached data.

        Returns Comment instances. CacheableModel handles field access via cache.
        Also prefills L0 cache for reply counts and author profiles.
        """
        if not ids:
            return []

        # Prefill L0 cache for comments, reply counts
        cached_results = _get_comment.batch([(id,) for id in ids])
        get_visible_reply_count.batch([(id,) for id in ids])

        # Collect author IDs and create instances
        author_ids = set()
        instances = []
        for comment_id, result in zip(ids, cached_results):
            if result is None:
                continue
            instance = cls(id=comment_id)
            instances.append(instance)
            if result.get("author_id"):
                author_ids.add(result["author_id"])

        # Prefill author profile cache
        if author_ids:
            from judge.models.profile import _get_profile

            _get_profile.batch([(aid,) for aid in author_ids])

        return instances

    @classmethod
    def prefill_vote_cache(cls, profile_id, comment_ids):
        """Prefill L0 cache for user votes on comments."""
        if profile_id and comment_ids:
            get_user_vote_on_comment.batch([(profile_id, cid) for cid in comment_ids])

    @property
    def count_replies(self):
        """Get visible reply count from cache."""
        return get_visible_reply_count(self.id)

    def get_vote_score(self, profile_id):
        """Get user's vote on this comment from cache."""
        if not profile_id:
            return 0
        return get_user_vote_on_comment(profile_id, self.id)

    # Getter methods for cached fields
    def get_body(self):
        return self.get_cached_value("body", "")

    def get_score(self):
        return self.get_cached_value("score", 0)

    def get_author_id(self):
        return self.get_cached_value("author_id")

    def get_hidden(self):
        return self.get_cached_value("hidden", False)

    def get_content_type_id(self):
        return self.get_cached_value("content_type_id")

    def get_object_id(self):
        return self.get_cached_value("object_id")

    def get_parent_id(self):
        return self.get_cached_value("parent_id")

    def get_revision_count(self):
        return self.get_cached_value("revision_count", 1)

    def get_time(self):
        """Return time as datetime object from cached ISO string."""
        from django.utils.dateparse import parse_datetime

        time_str = self.get_cached_value("time")
        if time_str:
            return parse_datetime(time_str)
        return None

    def get_reply_count(self):
        """Get visible reply count using cached function."""
        return get_visible_reply_count(self.id)

    @classmethod
    def filter_accessible(cls, queryset, user, n=None, batch=None):
        """
        Filter a queryset of comments to only include those with accessible linked objects.

        Args:
            queryset: Base queryset of comments to filter
            user: User to check accessibility for
            n: Maximum number of comments to return (-1 for all)
            batch: Batch size for processing (defaults to 2*n)

        Returns:
            List of accessible comments
        """
        problem_access = CacheDict(lambda p: p.is_accessible_by(user))
        contest_access = CacheDict(lambda c: c.is_accessible_by(user))
        blog_access = CacheDict(lambda b: b.is_accessible_by(user))

        if n == -1:
            n = len(queryset)
        if user.is_superuser:
            return list(queryset[:n])
        if batch is None:
            batch = 2 * n

        output = []
        for i in itertools.count(0):
            slice = queryset[i * batch : i * batch + batch]
            if not slice:
                break
            for comment in slice:
                if isinstance(comment.linked_object, Problem):
                    if problem_access[comment.linked_object]:
                        output.append(comment)
                elif isinstance(comment.linked_object, Contest):
                    if contest_access[comment.linked_object]:
                        output.append(comment)
                elif isinstance(comment.linked_object, BlogPost):
                    if blog_access[comment.linked_object]:
                        output.append(comment)
                elif isinstance(comment.linked_object, Solution):
                    if problem_access[comment.linked_object.problem]:
                        output.append(comment)
                if len(output) >= n:
                    return output
        return output

    @classmethod
    def most_recent(
        cls, user, view_type="all", content_filter="all", organization=None, n=None
    ):
        """
        Get accessible comments with optional filtering.

        Args:
            user: User to check accessibility for
            view_type: 'own' or 'all'
            content_filter: 'all', 'problem', 'contest', 'blog', 'other'
            organization: Organization to filter by
            n: Number of comments to return (None for QuerySet, number for filtered list)

        Returns:
            QuerySet if n is None, filtered list if n is provided
        """
        if view_type == "own" and user.is_authenticated:
            queryset = (
                cls.objects.filter(author=user.profile, hidden=False)
                .order_by("-time")
                .select_related("content_type")
            )
        else:
            queryset = (
                cls.objects.filter(hidden=False)
                .order_by("-time")
                .select_related("content_type")
            )

            if organization:
                queryset = queryset.filter(author__in=organization.members.all())

        # Apply content type filter
        if content_filter != "all":
            if content_filter == "problem":
                problem_ct = ContentType.objects.get_for_model(Problem)
                queryset = queryset.filter(content_type=problem_ct)
            elif content_filter == "contest":
                contest_ct = ContentType.objects.get_for_model(Contest)
                queryset = queryset.filter(content_type=contest_ct)
            elif content_filter == "blog":
                blog_ct = ContentType.objects.get_for_model(BlogPost)
                queryset = queryset.filter(content_type=blog_ct)
            elif content_filter == "other":
                problem_ct = ContentType.objects.get_for_model(Problem)
                contest_ct = ContentType.objects.get_for_model(Contest)
                blog_ct = ContentType.objects.get_for_model(BlogPost)
                queryset = queryset.exclude(
                    content_type__in=[problem_ct, contest_ct, blog_ct]
                )

        queryset = queryset.prefetch_related("linked_object")

        if view_type == "own" and user.is_authenticated:
            return queryset[:n]

        return cls.filter_accessible(queryset, user, n=n)

    @cached_property
    def get_replies(self):
        query = Comment.filter(parent=self)
        return len(query)

    @cached_property
    def page_title(self):
        linked_obj = self.linked_object
        if isinstance(linked_obj, Problem):
            return linked_obj.name
        elif isinstance(linked_obj, Contest):
            return linked_obj.name
        elif isinstance(linked_obj, Solution):
            return _("Editorial for ") + linked_obj.problem.name
        elif isinstance(linked_obj, BlogPost):
            return linked_obj.title

    @cached_property
    def link(self):
        linked_obj = self.linked_object
        if isinstance(linked_obj, Problem):
            return reverse("problem_detail", args=(linked_obj.code,))
        elif isinstance(linked_obj, Contest):
            return reverse("contest_view", args=(linked_obj.key,))
        elif isinstance(linked_obj, Solution):
            return reverse("problem_editorial", args=(linked_obj.problem.code,))
        elif isinstance(linked_obj, BlogPost):
            return reverse(
                "blog_post",
                args=(
                    self.object_id,
                    linked_obj.slug,
                ),
            )

    def get_absolute_url(self):
        return "%s?target_comment=%d#comment-%d" % (self.link, self.id, self.id)

    @classmethod
    def dirty_count_cache(cls, content_type_id, object_id):
        """Invalidate comment count caches for an object."""
        get_visible_comment_count.dirty(content_type_id, object_id)
        get_visible_top_level_comment_count.dirty(content_type_id, object_id)

    @classmethod
    def dirty_list_cache(cls, content_type_id, object_id, parent_id=None):
        """Invalidate comment list caches for an object."""
        # Dirty all sort combinations for top-level comments
        for sort_by in ("time", "score"):
            for sort_order in ("asc", "desc"):
                get_top_level_comment_ids.dirty(
                    content_type_id, object_id, sort_by, sort_order
                )
        # If this is a reply, also dirty the parent's reply list and count cache
        if parent_id:
            for sort_order in ("asc", "desc"):
                get_reply_ids.dirty(parent_id, sort_order)
            get_visible_reply_count.dirty(parent_id)

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            self.dirty_count_cache(self.content_type_id, self.object_id)
            self.dirty_list_cache(self.content_type_id, self.object_id, self.parent_id)

    def delete(self, *args, **kwargs):
        content_type_id = self.content_type_id
        object_id = self.object_id
        parent_id = self.parent_id
        super().delete(*args, **kwargs)
        self.dirty_count_cache(content_type_id, object_id)
        self.dirty_list_cache(content_type_id, object_id, parent_id)


class CommentVote(models.Model):
    voter = models.ForeignKey(Profile, related_name="voted_comments", on_delete=CASCADE)
    comment = models.ForeignKey(Comment, related_name="votes", on_delete=CASCADE)
    score = models.IntegerField()

    class Meta:
        unique_together = ["voter", "comment"]
        verbose_name = _("comment vote")
        verbose_name_plural = _("comment votes")


class CommentLock(models.Model):
    page = models.CharField(
        max_length=30,
        verbose_name=_("associated page"),
        db_index=True,
    )

    class Meta:
        permissions = (("override_comment_lock", _("Override comment lock")),)

    def __str__(self):
        return str(self.page)


@cache_wrapper(prefix="gcc2")
def get_visible_comment_count(content_type_id, object_id):
    """Get count of visible comments for an object."""
    return Comment.objects.filter(
        content_type_id=content_type_id, object_id=object_id, hidden=False
    ).count()


@cache_wrapper(prefix="gvtlcc2")
def get_visible_top_level_comment_count(content_type_id, object_id):
    """Get count of visible top-level comments."""
    return Comment.objects.filter(
        content_type_id=content_type_id, object_id=object_id, parent=None, hidden=False
    ).count()


def _get_user_vote_on_comment_batch(args_list):
    """Batch fetch user votes on comments."""
    if not args_list:
        return []

    # All args should have the same profile_id (typical use case)
    # But handle mixed profile_ids just in case
    profile_ids = set(args[0] for args in args_list)
    comment_ids = [args[1] for args in args_list]

    # Fetch all votes for these profile/comment pairs
    votes = CommentVote.objects.filter(
        voter_id__in=profile_ids, comment_id__in=comment_ids
    ).values_list("voter_id", "comment_id", "score")

    # Build lookup dict: (profile_id, comment_id) -> score
    vote_dict = {(v[0], v[1]): v[2] for v in votes}

    # Return results in order, defaulting to 0
    return [vote_dict.get((args[0], args[1]), 0) for args in args_list]


@cache_wrapper(prefix="ucvs", batch_fn=_get_user_vote_on_comment_batch)
def get_user_vote_on_comment(profile_id, comment_id):
    """Get a user's vote score on a comment (0 if not voted)."""
    try:
        return CommentVote.objects.get(voter_id=profile_id, comment_id=comment_id).score
    except CommentVote.DoesNotExist:
        return 0


def _get_visible_reply_count_batch(args_list):
    """Batch fetch visible reply counts for comments."""
    comment_ids = [args[0] for args in args_list]

    # Count visible replies for each comment
    from django.db.models import Count, Q

    counts = dict(
        Comment.objects.filter(id__in=comment_ids)
        .annotate(
            reply_count=Count("replies", distinct=True, filter=Q(replies__hidden=False))
        )
        .values_list("id", "reply_count")
    )

    return [counts.get(cid, 0) for cid in comment_ids]


@cache_wrapper(prefix="gvrc", batch_fn=_get_visible_reply_count_batch)
def get_visible_reply_count(comment_id):
    """Get count of visible replies for a comment."""
    return Comment.objects.filter(parent_id=comment_id, hidden=False).count()


@cache_wrapper(prefix="ctlcids")
def get_top_level_comment_ids(content_type_id, object_id, sort_by, sort_order):
    """
    Get cached list of top-level comment IDs for an object.

    Args:
        content_type_id: ContentType ID
        object_id: Object ID
        sort_by: 'time' or 'score'
        sort_order: 'asc' or 'desc'

    Returns:
        List of comment IDs in the requested order
    """
    queryset = Comment.objects.filter(
        content_type_id=content_type_id,
        object_id=object_id,
        parent=None,
        hidden=False,
    )

    if sort_by == "score":
        if sort_order == "desc":
            queryset = queryset.order_by("-score", "-time")
        else:
            queryset = queryset.order_by("score", "-time")
    else:  # time
        if sort_order == "desc":
            queryset = queryset.order_by("-time")
        else:
            queryset = queryset.order_by("time")

    return list(queryset.values_list("id", flat=True))


@cache_wrapper(prefix="crids")
def get_reply_ids(parent_comment_id, sort_order):
    """
    Get cached list of reply comment IDs for a parent comment.

    Args:
        parent_comment_id: Parent comment ID
        sort_order: 'asc' or 'desc'

    Returns:
        List of reply comment IDs in the requested order
    """
    queryset = Comment.objects.filter(
        parent_id=parent_comment_id,
        hidden=False,
    )

    if sort_order == "desc":
        queryset = queryset.order_by("-time")
    else:
        queryset = queryset.order_by("time")

    return list(queryset.values_list("id", flat=True))


# Batch function for Comment caching
def _get_comment_batch(args_list):
    """
    Batch fetch comment data for caching.

    Args:
        args_list: List of (comment_id,) tuples

    Returns:
        List of dictionaries with comment data, or None for deleted comments
    """
    comment_ids = [args[0] for args in args_list]

    # Use .values() instead of .only() to avoid triggering MPTT's __init__
    comments = Comment.objects.filter(id__in=comment_ids).values(
        "id",
        "body",
        "score",
        "time",
        "author_id",
        "hidden",
        "content_type_id",
        "object_id",
        "parent_id",
        "revision_count",
    )

    comment_dict = {}
    for comment in comments:
        comment_dict[comment["id"]] = {
            "body": comment["body"],
            "score": comment["score"],
            "time": comment["time"].isoformat() if comment["time"] else None,
            "author_id": comment["author_id"],
            "hidden": comment["hidden"],
            "content_type_id": comment["content_type_id"],
            "object_id": comment["object_id"],
            "parent_id": comment["parent_id"],
            "revision_count": comment["revision_count"],
        }

    results = []
    for comment_id in comment_ids:
        if comment_id in comment_dict:
            results.append(comment_dict[comment_id])
        else:
            # Comment was deleted, return None
            results.append(None)

    return results


@cache_wrapper(prefix="cmt2", batch_fn=_get_comment_batch)
def _get_comment(comment_id):
    """Get cached comment data dictionary."""
    results = _get_comment_batch([(comment_id,)])
    return results[0]


@cache_wrapper(prefix="ctaids")
def get_content_author_ids(content_type_id, object_id):
    """
    Get cached author IDs for a content object (BlogPost, Problem, etc.).
    Returns a set of profile IDs who are authors.
    """
    from django.contrib.contenttypes.models import ContentType

    try:
        content_type = ContentType.objects.get(id=content_type_id)
        model_class = content_type.model_class()
        obj = model_class.objects.get(id=object_id)

        # Use cached method if available
        if hasattr(obj, "get_author_ids"):
            return set(obj.get_author_ids())
        elif hasattr(obj, "authors"):
            return set(obj.authors.values_list("id", flat=True))
        return set()
    except (ContentType.DoesNotExist, model_class.DoesNotExist):
        return set()
