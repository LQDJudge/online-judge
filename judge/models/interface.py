import re

from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.functional import cached_property
from django.contrib.contenttypes.fields import GenericRelation
from mptt.fields import TreeForeignKey
from mptt.models import MPTTModel

from judge.models.profile import Organization, Profile
from judge.models.pagevote import PageVotable
from judge.models.bookmark import Bookmarkable
from judge.caching import cache_wrapper

__all__ = ["MiscConfig", "validate_regex", "NavigationBar", "BlogPost"]


class MiscConfig(models.Model):
    key = models.CharField(max_length=30, db_index=True)
    value = models.TextField(blank=True)

    def __str__(self):
        return self.key

    class Meta:
        verbose_name = _("configuration item")
        verbose_name_plural = _("miscellaneous configuration")


def validate_regex(regex):
    try:
        re.compile(regex, re.VERBOSE)
    except re.error as e:
        raise ValidationError("Invalid regex: %s" % e.message)


class NavigationBar(MPTTModel):
    class Meta:
        verbose_name = _("navigation item")
        verbose_name_plural = _("navigation bar")

    class MPTTMeta:
        order_insertion_by = ["order"]

    order = models.PositiveIntegerField(db_index=True, verbose_name=_("order"))
    key = models.CharField(max_length=10, unique=True, verbose_name=_("identifier"))
    label = models.CharField(max_length=20, verbose_name=_("label"))
    path = models.CharField(max_length=255, verbose_name=_("link path"))
    regex = models.TextField(
        verbose_name=_("highlight regex"), validators=[validate_regex]
    )
    parent = TreeForeignKey(
        "self",
        verbose_name=_("parent item"),
        null=True,
        blank=True,
        related_name="children",
        on_delete=models.CASCADE,
    )

    def __str__(self):
        return self.label

    @property
    def pattern(self, cache={}):
        # A cache with a bad policy is an alias for memory leak
        # Thankfully, there will never be too many regexes to cache.
        if self.regex in cache:
            return cache[self.regex]
        else:
            pattern = cache[self.regex] = re.compile(self.regex, re.VERBOSE)
            return pattern


class BlogPost(models.Model, PageVotable, Bookmarkable):
    title = models.CharField(verbose_name=_("post title"), max_length=100)
    authors = models.ManyToManyField(Profile, verbose_name=_("authors"), blank=True)
    slug = models.SlugField(verbose_name=_("slug"))
    visible = models.BooleanField(verbose_name=_("public visibility"), default=False)
    sticky = models.BooleanField(verbose_name=_("sticky"), default=False)
    publish_on = models.DateTimeField(verbose_name=_("publish after"))
    content = models.TextField(verbose_name=_("post content"))
    summary = models.TextField(verbose_name=_("post summary"), blank=True)
    og_image = models.CharField(
        verbose_name=_("openGraph image"), default="", max_length=150, blank=True
    )
    organizations = models.ManyToManyField(
        Organization,
        blank=True,
        verbose_name=_("organizations"),
        help_text=_("If private, only these organizations may see the blog post."),
    )
    is_organization_private = models.BooleanField(
        verbose_name=_("private to organizations"), default=False
    )
    is_rejected = models.BooleanField(
        verbose_name=_("rejected"),
        default=False,
        help_text=_("True if post was rejected by moderator"),
    )
    comments = GenericRelation("Comment")
    pagevote = GenericRelation("PageVote")
    bookmark = GenericRelation("BookMark")
    tickets = GenericRelation("Ticket")

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("blog_post", args=(self.id, self.slug))

    def is_accessible_by(self, user):
        if self.visible and self.publish_on <= timezone.now():
            if not self.is_organization_private:
                return True
            if self.organizations.filter(is_open=True).exists():
                return True
            if (
                user.is_authenticated
                and self.organizations.filter(
                    id__in=user.profile.organizations.all()
                ).exists()
            ):
                return True
        if user.has_perm("judge.edit_all_post"):
            return True
        return (
            user.is_authenticated and self.authors.filter(id=user.profile.id).exists()
        )

    def is_editable_by(self, user):
        if not user.is_authenticated:
            return False
        if user.has_perm("judge.edit_all_post"):
            return True
        return (
            user.has_perm("judge.change_blogpost")
            and self.authors.filter(id=user.profile.id).exists()
        )

    @cache_wrapper(prefix="BPgai", expected_type=list)
    def get_author_ids(self):
        return list(self.authors.values_list("id", flat=True))

    def get_authors(self):
        return Profile.get_cached_instances(*self.get_author_ids())

    def get_organization_ids(self):
        return _get_blogpost_organization_ids(self.id)

    @classmethod
    def prefetch_organization_ids(cls, *blogpost_ids):
        _get_blogpost_organization_ids.batch([(id,) for id in blogpost_ids])

    def get_organizations(self):
        organization_ids = self.get_organization_ids()
        return Organization.get_cached_instances(*organization_ids)

    class Meta:
        permissions = (("edit_all_post", _("Edit all posts")),)
        verbose_name = _("blog post")
        verbose_name_plural = _("blog posts")


def _get_blogpost_organization_ids_batch(args_list):
    """
    Batch function to get organization IDs for multiple blog posts efficiently.

    Args:
        args_list: List of tuples, each containing a single blogpost_id

    Returns:
        List of organization ID lists, one for each blogpost_id in args_list
    """
    # Extract blog post IDs from args_list
    blogpost_ids = [args[0] for args in args_list]

    # Direct query to the through table to avoid JOIN
    through_model = BlogPost.organizations.through
    query = through_model.objects.filter(blogpost_id__in=blogpost_ids)

    # Group organization IDs by blog post ID
    blogpost_orgs = {}
    for blogpost_id, org_id in query.values_list("blogpost_id", "organization_id"):
        if blogpost_id not in blogpost_orgs:
            blogpost_orgs[blogpost_id] = []
        blogpost_orgs[blogpost_id].append(org_id)

    # Return results in the same order as input blogpost_ids
    results = []
    for blogpost_id in blogpost_ids:
        results.append(blogpost_orgs.get(blogpost_id, []))

    return results


@cache_wrapper(
    prefix="BPgoi", expected_type=list, batch_fn=_get_blogpost_organization_ids_batch
)
def _get_blogpost_organization_ids(blogpost_id):
    """Get organization IDs for a blog post"""
    results = _get_blogpost_organization_ids_batch([(blogpost_id,)])
    return results[0]
