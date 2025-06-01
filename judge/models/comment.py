import itertools

from django.contrib.contenttypes.fields import GenericRelation
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.core.validators import RegexValidator
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
from judge.caching import cache_wrapper


__all__ = ["Comment", "CommentLock", "CommentVote", "Notification"]


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


class Comment(MPTTModel):
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


@cache_wrapper(prefix="gcc")
def get_visible_comment_count(content_type, object_id):
    return Comment.objects.filter(
        content_type=content_type, object_id=object_id, hidden=False
    ).count()


@cache_wrapper(prefix="gvtlcc")
def get_visible_top_level_comment_count(content_type, object_id):
    return Comment.objects.filter(
        content_type=content_type, object_id=object_id, parent=None, hidden=False
    ).count()
