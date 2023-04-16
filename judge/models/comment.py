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
    
    versions = VersionRelation()

    class Meta:
        verbose_name = _("comment")
        verbose_name_plural = _("comments")
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    class MPTTMeta:
        order_insertion_by = ["-time"]

    @classmethod
    def most_recent(cls, user, n, batch=None, organization=None):
        queryset = (
            cls.objects.filter(hidden=False)
            .select_related("author__user")
            .defer("author__about", "body")
            .order_by("-id")
        )

        if organization:
            queryset = queryset.filter(author__in=organization.members.all())

        problem_access = CacheDict(lambda p: p.is_accessible_by(user))
        contest_access = CacheDict(lambda c: c.is_accessible_by(user))
        blog_access = CacheDict(lambda b: b.can_see(user))

        if n == -1:
            n = len(queryset)
        if user.is_superuser:
            return queryset[:n]
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
    
    @cached_property
    def get_replies(self):
        query = Comment.filter(parent=self)
        return len(query)

    @cached_property
    def get_revisions(self):
        return self.versions.count()

    @cached_property
    def page_title(self):
        if isinstance(self.linked_object, Problem):
            return self.linked_object.name
        elif isinstance(self.linked_object, Contest):
            return self.linked_object.name
        elif isinstance(self.linked_object, Solution):
            return _("Editorial for ") + self.linked_object.problem.name
        elif isinstance(self.linked_object, BlogPost):
            return self.linked_object.title

    @cached_property
    def link(self):
        if isinstance(self.linked_object, Problem):
            return reverse("problem_detail", args=(self.linked_object.code,))
        elif isinstance(self.linked_object, Contest):
            return reverse("contest_view", args=(self.linked_object.key,))
        elif isinstance(self.linked_object, Solution):
            return reverse("problem_editorial", args=(self.linked_object.problem.code,))
        elif isinstance(self.linked_object, BlogPost):
            return reverse(
                "blog_post",
                args=(
                    self.object_id,
                    self.linked_object.slug,
                ),
            )

    def get_absolute_url(self):
        return "%s#comment-%d" % (self.link, self.id)


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


class Notification(models.Model):
    owner = models.ForeignKey(
        Profile,
        verbose_name=_("owner"),
        related_name="notifications",
        on_delete=CASCADE,
    )
    time = models.DateTimeField(verbose_name=_("posted time"), auto_now_add=True)
    comment = models.ForeignKey(
        Comment, null=True, verbose_name=_("comment"), on_delete=CASCADE
    )
    read = models.BooleanField(verbose_name=_("read"), default=False)
    category = models.CharField(verbose_name=_("category"), max_length=1000)
    html_link = models.TextField(
        default="",
        verbose_name=_("html link to comments, used for non-comments"),
        max_length=1000,
    )
    author = models.ForeignKey(
        Profile,
        null=True,
        verbose_name=_("who trigger, used for non-comment"),
        on_delete=CASCADE,
    )
