from django.contrib.contenttypes.models import ContentType
from django.db.models import Sum, Q

from judge.models.pagevote import PageVote
from judge.models.comment import Comment


def _get_public_content_ids_by_author(profile):
    """
    Returns dict of {ContentType: [list of object IDs]} for all public content
    authored by the given profile.
    """
    from judge.models import BlogPost, Contest, Problem, Solution
    from judge.models.profile import Organization

    result = {}

    # Public Solutions
    ct = ContentType.objects.get_for_model(Solution)
    ids = list(
        Solution.objects.filter(is_public=True, authors=profile).values_list(
            "id", flat=True
        )
    )
    if ids:
        result[ct] = ids

    # Public Contests
    ct = ContentType.objects.get_for_model(Contest)
    ids = list(
        Contest.objects.filter(
            is_visible=True,
            is_private=False,
            is_organization_private=False,
            is_in_course=False,
            authors=profile,
        ).values_list("id", flat=True)
    )
    if ids:
        result[ct] = ids

    # Public Problems
    ct = ContentType.objects.get_for_model(Problem)
    ids = list(
        Problem.objects.filter(
            is_public=True, is_organization_private=False, authors=profile
        ).values_list("id", flat=True)
    )
    if ids:
        result[ct] = ids

    # Public Blog Posts (non-org-private)
    blog_ct = ContentType.objects.get_for_model(BlogPost)
    blog_ids = list(
        BlogPost.objects.filter(
            visible=True, is_organization_private=False, authors=profile
        ).values_list("id", flat=True)
    )

    # Community Blog Posts (org-private in community orgs)
    community_org_ids = list(
        Organization.objects.filter(is_community=True).values_list("id", flat=True)
    )
    if community_org_ids:
        community_blog_ids = list(
            BlogPost.objects.filter(
                visible=True,
                is_organization_private=True,
                organizations__in=community_org_ids,
                authors=profile,
            )
            .distinct()
            .values_list("id", flat=True)
        )
        blog_ids = list(set(blog_ids) | set(community_blog_ids))

    if blog_ids:
        result[blog_ct] = blog_ids

    return result


def _get_all_public_content_ids():
    """
    Returns dict of {ContentType: [list of object IDs]} for all public content.
    Used to determine which content comments can count toward contribution.
    """
    from judge.models import BlogPost, Contest, Problem, Solution
    from judge.models.profile import Organization

    result = {}

    ct = ContentType.objects.get_for_model(Solution)
    ids = list(Solution.objects.filter(is_public=True).values_list("id", flat=True))
    if ids:
        result[ct] = ids

    ct = ContentType.objects.get_for_model(Contest)
    ids = list(
        Contest.objects.filter(
            is_visible=True,
            is_private=False,
            is_organization_private=False,
            is_in_course=False,
        ).values_list("id", flat=True)
    )
    if ids:
        result[ct] = ids

    ct = ContentType.objects.get_for_model(Problem)
    ids = list(
        Problem.objects.filter(
            is_public=True, is_organization_private=False
        ).values_list("id", flat=True)
    )
    if ids:
        result[ct] = ids

    blog_ct = ContentType.objects.get_for_model(BlogPost)
    blog_ids = list(
        BlogPost.objects.filter(
            visible=True, is_organization_private=False
        ).values_list("id", flat=True)
    )

    community_org_ids = list(
        Organization.objects.filter(is_community=True).values_list("id", flat=True)
    )
    if community_org_ids:
        community_blog_ids = list(
            BlogPost.objects.filter(
                visible=True,
                is_organization_private=True,
                organizations__in=community_org_ids,
            )
            .distinct()
            .values_list("id", flat=True)
        )
        blog_ids = list(set(blog_ids) | set(community_blog_ids))

    if blog_ids:
        result[blog_ct] = blog_ids

    return result


def compute_contribution(profile):
    """
    Compute global contribution points for a profile.

    contribution_points = sum of (upvotes - downvotes) across all public content
    authored by the user (PageVote scores) plus sum of Comment.score for
    comments authored by the user on any public/community content.
    """
    total = 0

    # 1. PageVote scores for content authored by the user
    authored_content = _get_public_content_ids_by_author(profile)
    for ct, ids in authored_content.items():
        pagevote_sum = (
            PageVote.objects.filter(
                content_type=ct,
                object_id__in=ids,
            ).aggregate(
                total=Sum("score")
            )["total"]
            or 0
        )
        total += pagevote_sum

    # 2. Comment scores for comments by the user on any public content
    all_public_content = _get_all_public_content_ids()
    for ct, ids in all_public_content.items():
        comment_sum = (
            Comment.objects.filter(
                content_type=ct,
                object_id__in=ids,
                author=profile,
                hidden=False,
            ).aggregate(total=Sum("score"))["total"]
            or 0
        )
        total += comment_sum

    return total


def is_content_public(content_type, object_id):
    """
    Check if a piece of content passes the public/community visibility checks.
    Returns (is_public, author_profile_id_or_None).
    """
    from judge.models import BlogPost, Contest, Problem, Solution

    model_class = content_type.model_class()

    try:
        obj = model_class.objects.get(id=object_id)
    except model_class.DoesNotExist:
        return False

    if model_class == Solution:
        return obj.is_public
    elif model_class == Contest:
        return (
            obj.is_visible
            and not obj.is_private
            and not obj.is_organization_private
            and not obj.is_in_course
        )
    elif model_class == Problem:
        return obj.is_public and not obj.is_organization_private
    elif model_class == BlogPost:
        if obj.visible and not obj.is_organization_private:
            return True
        if obj.visible and obj.is_organization_private:
            return obj.organizations.filter(is_community=True).exists()
        return False

    return False


def get_content_author_profile_ids(content_type, object_id):
    """Get author profile IDs for a piece of content."""
    model_class = content_type.model_class()

    try:
        obj = model_class.objects.get(id=object_id)
    except model_class.DoesNotExist:
        return []

    if hasattr(obj, "authors"):
        return list(obj.authors.values_list("id", flat=True))
    return []


def bulk_compute_contributions():
    """
    Compute contribution_points for ALL profiles in bulk using aggregated queries.
    Returns dict of {profile_id: contribution_points}.
    Much faster than calling compute_contribution() per profile.
    """
    from collections import defaultdict
    from judge.models import BlogPost, Contest, Problem, Solution
    from judge.models.profile import Organization

    scores = defaultdict(int)

    # Build public content sets per model
    content_configs = [
        (Solution, Q(is_public=True)),
        (
            Contest,
            Q(
                is_visible=True,
                is_private=False,
                is_organization_private=False,
                is_in_course=False,
            ),
        ),
        (Problem, Q(is_public=True, is_organization_private=False)),
        (BlogPost, Q(visible=True, is_organization_private=False)),
    ]

    # Community blog posts
    community_org_ids = list(
        Organization.objects.filter(is_community=True).values_list("id", flat=True)
    )

    for model, public_filter in content_configs:
        ct = ContentType.objects.get_for_model(model)
        public_ids = set(
            model.objects.filter(public_filter).values_list("id", flat=True)
        )

        # Add community blogs
        if model == BlogPost and community_org_ids:
            community_blog_ids = set(
                BlogPost.objects.filter(
                    visible=True,
                    is_organization_private=True,
                    organizations__in=community_org_ids,
                )
                .distinct()
                .values_list("id", flat=True)
            )
            public_ids |= community_blog_ids

        if not public_ids:
            continue

        # 1. PageVote scores -> credit to content authors
        # Get (object_id, score) for public content
        pagevotes = PageVote.objects.filter(
            content_type=ct,
            object_id__in=public_ids,
        ).values_list("object_id", "score")

        # Map object_id -> score
        obj_scores = {}
        for obj_id, score in pagevotes:
            obj_scores[obj_id] = score

        # Get author mappings: author_id -> [object_ids]
        if obj_scores:
            # Query the M2M authors table
            author_pairs = model.authors.through.objects.filter(
                **{f"{model.__name__.lower()}_id__in": list(obj_scores.keys())}
            ).values_list(f"{model.__name__.lower()}_id", "profile_id")
            for obj_id, author_id in author_pairs:
                if obj_id in obj_scores:
                    scores[author_id] += obj_scores[obj_id]

        # 2. Comment scores -> credit to comment authors
        comment_scores = (
            Comment.objects.filter(
                content_type=ct,
                object_id__in=public_ids,
                hidden=False,
            )
            .values("author_id")
            .annotate(total=Sum("score"))
        )
        for row in comment_scores:
            if row["author_id"] and row["total"]:
                scores[row["author_id"]] += row["total"]

    # Clamp to IntegerField range
    INT_MAX = 2147483647
    INT_MIN = -2147483648
    for pid in scores:
        scores[pid] = max(INT_MIN, min(INT_MAX, scores[pid]))

    return dict(scores)
