"""
Data source pools for the feed generator.
Each pool provides items of a specific type, lazily fetched on first access.
"""

from datetime import timedelta

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db.models import Count, Q
from django.utils import timezone

from judge.models import BlogPost, Contest, Problem
from judge.models.comment import Comment
from judge.models.course import Course
from judge.models.problem import Solution
from judge.models.profile import Organization
from judge.models.quiz import Quiz

from .items import FeedItem

FEED_CACHE_TTL = 60 * 60  # 1 hour


def _feed_cache_key(request, pool_name):
    """Cache key scoped to user + feed token (from ?ft= param) + org context."""
    if request.user.is_authenticated:
        user_key = f"u{request.profile.id}"
    else:
        user_key = "anon"
    feed_token = request.GET.get("ft", "")
    return f"feed:{pool_name}:{user_key}:{feed_token}"


class CachedPool:
    """
    Base class for pools with session-scoped caching.
    Subclasses implement _fetch() to return a list of FeedItems.
    Items are cached per session for 1 hour. Lazy: only builds on first get().
    """

    pool_name = None  # override in subclass

    def __init__(self, request, organization=None):
        self.request = request
        self.organization = organization
        self._items = None
        self._cache_key = _feed_cache_key(request, self.pool_name)

    def get(self, offset, count):
        self._build()
        return self._items[offset : offset + count]

    def has_more(self, offset):
        self._build()
        return offset < len(self._items)

    def _build(self):
        if self._items is not None:
            return
        cached = cache.get(self._cache_key)
        if cached is not None:
            self._items = cached
            return
        self._items = self._fetch()
        cache.set(self._cache_key, self._items, FEED_CACHE_TTL)

    def _fetch(self):
        """Override in subclass. Return list of FeedItem."""
        raise NotImplementedError


class PostPool(CachedPool):
    """
    Blog posts: combines official + group + community posts,
    sorted by sticky then publish time. Overfetches once, cached per session.
    """

    pool_name = "post"
    BATCH_SIZE = 100

    def _fetch(self):

        now = timezone.now()
        base = BlogPost.objects.filter(visible=True, publish_on__lte=now)

        if self.organization:
            base = base.filter(organizations=self.organization)

        if not self.request.user.is_authenticated:
            # Anonymous — official posts only
            posts = list(
                base.filter(is_organization_private=False).order_by(
                    "-sticky", "-publish_on"
                )[: self.BATCH_SIZE]
            )
        else:
            # Combine official + group + community, dedup
            official = set(
                base.filter(is_organization_private=False)
                .order_by("-sticky", "-publish_on")
                .values_list("id", flat=True)[: self.BATCH_SIZE]
            )

            user_org_ids = self.request.profile.get_organization_ids()
            group = set(
                base.filter(
                    is_organization_private=True,
                    organizations__in=user_org_ids,
                )
                .order_by("-publish_on")
                .values_list("id", flat=True)[: self.BATCH_SIZE]
            )

            community = set(
                base.filter(
                    is_organization_private=True,
                    organizations__is_community=True,
                )
                .order_by("-publish_on")
                .values_list("id", flat=True)[: self.BATCH_SIZE]
            )

            all_ids = official | group | community
            posts = list(
                base.filter(id__in=all_ids).order_by("-sticky", "-publish_on")[
                    : self.BATCH_SIZE
                ]
            )

        BlogPost.prefetch_organization_ids(*[p.id for p in posts])
        return [FeedItem(FeedItem.POST, post, time=post.publish_on) for post in posts]


class CommentPool(CachedPool):
    """Comment activity, grouped/deduped by parent object."""

    pool_name = "comment"
    BATCH_SIZE = 50

    def _fetch(self):
        org = self.organization

        if org:
            # Org context: comments on org content (posts, problems, contests, solutions)
            blog_ct = ContentType.objects.get_for_model(BlogPost)
            problem_ct = ContentType.objects.get_for_model(Problem)
            contest_ct = ContentType.objects.get_for_model(Contest)
            solution_ct = ContentType.objects.get_for_model(Solution)

            org_post_ids = set(
                BlogPost.objects.filter(organizations=org).values_list("id", flat=True)
            )
            org_problem_ids = set(
                Problem.objects.filter(organizations=org).values_list("id", flat=True)
            )
            org_contest_ids = set(
                Contest.objects.filter(organizations=org).values_list("id", flat=True)
            )
            org_solution_ids = set(
                Solution.objects.filter(problem__organizations=org).values_list(
                    "id", flat=True
                )
            )

            comments = list(
                Comment.objects.filter(hidden=False)
                .filter(
                    Q(content_type=blog_ct, object_id__in=org_post_ids)
                    | Q(content_type=problem_ct, object_id__in=org_problem_ids)
                    | Q(content_type=contest_ct, object_id__in=org_contest_ids)
                    | Q(content_type=solution_ct, object_id__in=org_solution_ids)
                )
                .select_related("author", "content_type")
                .prefetch_related("linked_object")
                .order_by("-time")[: self.BATCH_SIZE]
            )
        else:
            comments = Comment.most_recent(
                user=self.request.user,
                n=self.BATCH_SIZE,
            )

        blog_ct = ContentType.objects.get_for_model(BlogPost)

        # Group by parent object, dedup authors
        groups = {}
        for comment in comments:
            # Skip comments on blog posts (those are shown in PostPool)
            if not org and comment.content_type_id == blog_ct.id:
                continue
            parent_key = (comment.content_type_id, comment.object_id)
            if parent_key not in groups:
                groups[parent_key] = {
                    "commenter_ids": [],
                    "latest_time": comment.time,
                    "comment": comment,
                }
            if comment.author_id not in groups[parent_key]["commenter_ids"]:
                groups[parent_key]["commenter_ids"].append(comment.author_id)

        # Batch-fetch all commenter profiles from cache
        all_author_ids = set()
        for group in groups.values():
            all_author_ids.update(group["commenter_ids"])

        from judge.models.profile import Profile

        cached_profiles = Profile.get_cached_instances(*all_author_ids)
        profiles = {p.id: p for p in cached_profiles}

        for group in groups.values():
            group["commenters"] = [
                profiles[aid] for aid in group["commenter_ids"] if aid in profiles
            ]

        return [
            FeedItem(FeedItem.COMMENT, group, time=group["latest_time"])
            for group in groups.values()
        ]


class ProblemPool(CachedPool):
    """Recommended problems."""

    pool_name = "problem"

    def _fetch(self):
        org = self.organization
        if org:
            problems = Problem.objects.filter(
                organizations=org, is_public=True
            ).order_by("-date")[:100]
            return [FeedItem(FeedItem.PROBLEM, p) for p in problems]

        try:
            from judge.utils.problems import (
                get_user_recommended_problems,
                hot_problems,
                RecommendationType,
                user_completed_ids,
            )

            visible_ids = list(
                Problem.get_visible_problems(self.request.user).values_list(
                    "id", flat=True
                )
            )
            if not visible_ids:
                return []

            if self.request.user.is_authenticated:
                solved = user_completed_ids(self.request.profile)
                visible_ids = [pid for pid in visible_ids if pid not in solved]

            if not visible_ids:
                return []

            if self.request.user.is_authenticated and getattr(
                settings, "USE_ML", False
            ):
                rec_ids = get_user_recommended_problems(
                    self.request.profile.id,
                    visible_ids,
                    [
                        RecommendationType.TWO_TOWER,
                        RecommendationType.CF,
                        RecommendationType.HOT_PROBLEM,
                    ],
                    [200, 100, 100],
                    shuffle=True,
                )
            else:
                hot = hot_problems(timedelta(days=30), 100)
                rec_ids = [p.id for p in hot] if hot else []

            if not rec_ids:
                return []

            Problem.prefetch_cache_description(
                getattr(self.request, "LANGUAGE_CODE", "en"), *rec_ids[:100]
            )
            problems = Problem.get_cached_instances(*rec_ids[:100])
            return [FeedItem(FeedItem.PROBLEM, p) for p in problems]
        except Exception:
            return []


class ContestPool(CachedPool):
    """Recommended contests."""

    pool_name = "contest"

    def _fetch(self):

        org = self.organization
        if org:
            # Organization mode: recent org contests
            contests = Contest.objects.filter(
                is_visible=True,
                is_organization_private=True,
                organizations=org,
            ).order_by("-start_time")[:50]
            return [FeedItem(FeedItem.CONTEST, c) for c in contests]

        try:
            from judge.utils.contest_recommendation import (
                get_recommended_contests,
                get_recommended_contests_for_anonymous,
            )

            if self.request.user.is_authenticated:
                rec_ids = get_recommended_contests(self.request.profile, limit=50)
            else:
                rec_ids = get_recommended_contests_for_anonymous(limit=50)

            if not rec_ids:
                return []

            clean_ids = [r[0] if isinstance(r, (list, tuple)) else r for r in rec_ids]
            contests = Contest.objects.filter(id__in=clean_ids)
            id_map = {c.id: c for c in contests}
            return [
                FeedItem(FeedItem.CONTEST, id_map[cid])
                for cid in clean_ids
                if cid in id_map
            ]
        except Exception:
            return []


class GroupCardPool:
    """Courses, quizzes, groups to join. Cycles through card types. Cached per session."""

    def __init__(self, request, organization=None):
        self.request = request
        self.organization = organization
        self._cards = None
        self._cache_key = _feed_cache_key(request, "group_card")

    def get(self, offset, count):
        self._build()
        return self._cards[offset : offset + count]

    def has_more(self, offset):
        self._build()
        return offset < len(self._cards)

    def _build(self):
        if self._cards is not None:
            return
        cached = cache.get(self._cache_key)
        if cached is not None:
            self._cards = cached
            return
        self._cards = self._fetch()
        cache.set(self._cache_key, self._cards, FEED_CACHE_TTL)

    def _fetch(self):
        cards = []

        org = self.organization
        if org:
            return cards

        try:
            if self.request.user.is_authenticated:
                courses = list(Course.get_user_courses(self.request.profile)[:3])
                if not courses:
                    courses = list(
                        Course.get_joinable_courses(self.request.profile)[:3]
                    )
            else:
                courses = list(Course.objects.filter(is_public=True, is_open=True)[:3])
            if courses:
                cards.append(FeedItem(FeedItem.COURSES, courses))
        except Exception:
            pass

        try:
            quizzes = list(
                Quiz.objects.filter(is_public=True)
                .annotate(question_count=Count("quiz_questions"))
                .order_by("-id")[:3]
            )
            if quizzes:
                cards.append(FeedItem(FeedItem.QUIZZES, quizzes))
        except Exception:
            pass

        try:
            if self.request.user.is_authenticated:
                user_org_ids = set(
                    self.request.profile.organizations.values_list("id", flat=True)
                )
                groups = list(
                    Organization.objects.filter(is_open=True)
                    .exclude(id__in=user_org_ids)
                    .annotate(num_members=Count("member"))
                    .order_by("-num_members")[:4]
                )
            else:
                groups = list(
                    Organization.objects.filter(is_open=True)
                    .annotate(num_members=Count("member"))
                    .order_by("-num_members")[:4]
                )
            if groups:
                cards.append(FeedItem(FeedItem.GROUPS, groups))
        except Exception:
            pass

        return cards
