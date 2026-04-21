from collections import defaultdict

from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Sum, Q

from judge.models import BlogPost, Contest, Problem, Solution
from judge.models.pagevote import (
    PageVote,
    PageVoteVoter,
    dirty_pagevote,
)
from judge.models.comment import (
    Comment,
    CommentVote,
    get_user_vote_on_comment,
)
from judge.models.profile import Organization


def _get_public_content_ids_by_author(profile):
    """
    Returns dict of {ContentType: [list of object IDs]} for all public content
    authored by the given profile.
    """
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


# A downvote on content whose own score >= POPULAR_TARGET_SCORE is contrarian:
# the community has already endorsed it. Repeated such votes at scale catch
# abusers who dilute their overall ratio with a handful of upvotes and slip
# past the ratio-only rule.
POPULAR_TARGET_SCORE = 3
POPULAR_DOWNVOTE_THRESHOLD = 30


def detect_abusive_downvoters(min_downvotes=20, max_up_ratio=0.10):
    """
    Return {voter_id: {"down", "up", "popular", "signals": [...]}} for every
    voter flagged by at least one rule (signals are OR'd):
      - "ratio":   downvotes >= min_downvotes AND
                   upvotes <= max_up_ratio * downvotes
      - "popular": >= POPULAR_DOWNVOTE_THRESHOLD downvotes on content whose
                   own score >= POPULAR_TARGET_SCORE

    A dict is returned (not a set) so callers can report why each voter was
    flagged and display per-voter totals. The returned object is falsy when
    empty, iterates over voter IDs, and supports `in` / `voter_id__in=` —
    so existing callers keep working.
    """
    counts = defaultdict(lambda: {"up": 0, "down": 0, "popular": 0})

    # Overall up / down across both vote tables.
    for table in (PageVoteVoter, CommentVote):
        rows = table.objects.values("voter_id").annotate(
            up=Count("id", filter=Q(score=1)),
            down=Count("id", filter=Q(score=-1)),
        )
        for row in rows:
            counts[row["voter_id"]]["up"] += row["up"]
            counts[row["voter_id"]]["down"] += row["down"]

    # Downvotes on comments with community-endorsed scores.
    # Note: this compares against the comment's *current* score, which already
    # reflects the abuser's own -1. For comments dogpiled below threshold this
    # undercounts the popular signal — an inherent limitation without
    # timestamped votes. The threshold (30) is generous enough that serial
    # abusers still trip the rule; acknowledge and move on.
    popular_cmt_rows = (
        CommentVote.objects.filter(score=-1, comment__score__gte=POPULAR_TARGET_SCORE)
        .values("voter_id")
        .annotate(n=Count("id"))
    )
    for row in popular_cmt_rows:
        counts[row["voter_id"]]["popular"] += row["n"]

    # Downvotes on pages (blogs/problems/contests/solutions) with positive score.
    popular_page_rows = (
        PageVoteVoter.objects.filter(
            score=-1, pagevote__score__gte=POPULAR_TARGET_SCORE
        )
        .values("voter_id")
        .annotate(n=Count("id"))
    )
    for row in popular_page_rows:
        counts[row["voter_id"]]["popular"] += row["n"]

    flagged = {}
    for voter_id, c in counts.items():
        signals = []
        if c["down"] >= min_downvotes and c["up"] <= max_up_ratio * c["down"]:
            signals.append("ratio")
        if c["popular"] >= POPULAR_DOWNVOTE_THRESHOLD:
            signals.append("popular")
        if signals:
            flagged[voter_id] = {
                "down": c["down"],
                "up": c["up"],
                "popular": c["popular"],
                "signals": signals,
            }
    return flagged


def purge_downvotes_from(flagged_voter_ids):
    """
    Delete score=-1 PageVoteVoter + CommentVote rows from the given voter IDs,
    re-sum PageVote.score / Comment.score, and dirty the same caches the
    normal vote path dirties.

    Returns a dict of stats:
      {
        "pagevote_deleted": int,
        "commentvote_deleted": int,
        "pagevotes_rescored": int,
        "comments_rescored": int,
      }

    The caller is responsible for dirtying get_contribution_rank and
    recomputing Profile.contribution_points.
    """
    stats = {
        "pagevote_deleted": 0,
        "commentvote_deleted": 0,
        "pagevotes_rescored": 0,
        "comments_rescored": 0,
    }

    if not flagged_voter_ids:
        return stats

    # Snapshot what we're about to touch before deletion.
    affected_pv_ids = set(
        PageVoteVoter.objects.filter(
            voter_id__in=flagged_voter_ids,
            score=-1,
        ).values_list("pagevote_id", flat=True)
    )

    affected_voter_comment_pairs = list(
        CommentVote.objects.filter(
            voter_id__in=flagged_voter_ids,
            score=-1,
        ).values_list("voter_id", "comment_id")
    )
    affected_comment_ids = {c for _, c in affected_voter_comment_pairs}

    parent_triples = set()
    if affected_comment_ids:
        for ct_id, obj_id, parent_id in Comment.objects.filter(
            id__in=affected_comment_ids
        ).values_list("content_type_id", "object_id", "parent_id"):
            parent_triples.add((ct_id, obj_id, parent_id))

    # Delete the -1 rows.
    stats["pagevote_deleted"], _ = PageVoteVoter.objects.filter(
        voter_id__in=flagged_voter_ids,
        score=-1,
    ).delete()
    stats["commentvote_deleted"], _ = CommentVote.objects.filter(
        voter_id__in=flagged_voter_ids,
        score=-1,
    ).delete()

    # Re-sum PageVote.score for affected pagevotes (bulk_update = one UPDATE).
    if affected_pv_ids:
        pv_sums = dict(
            PageVoteVoter.objects.filter(pagevote_id__in=affected_pv_ids)
            .values("pagevote_id")
            .annotate(total=Sum("score"))
            .values_list("pagevote_id", "total")
        )
        pagevotes_to_update = []
        for pv in PageVote.objects.filter(id__in=affected_pv_ids):
            pv.score = pv_sums.get(pv.id) or 0
            pagevotes_to_update.append(pv)
        PageVote.objects.bulk_update(pagevotes_to_update, ["score"], batch_size=500)
        stats["pagevotes_rescored"] = len(pagevotes_to_update)

    # Re-sum Comment.score for affected comments.
    if affected_comment_ids:
        cv_sums = dict(
            CommentVote.objects.filter(comment_id__in=affected_comment_ids)
            .values("comment_id")
            .annotate(total=Sum("score"))
            .values_list("comment_id", "total")
        )
        comments_to_update = []
        for c in Comment.objects.filter(id__in=affected_comment_ids):
            c.score = cv_sums.get(c.id) or 0
            comments_to_update.append(c)
        Comment.objects.bulk_update(comments_to_update, ["score"], batch_size=500)
        stats["comments_rescored"] = len(comments_to_update)

    # Dirty caches using the same helpers the normal vote flow uses.
    # Single query for pagevotes, single delete_many for user-vote cache.
    for pv in PageVote.objects.filter(id__in=affected_pv_ids).select_related(
        "content_type"
    ):
        dirty_pagevote(pv)

    if affected_comment_ids:
        Comment.dirty_cache(*affected_comment_ids)
    for ct_id, obj_id, parent_id in parent_triples:
        Comment.dirty_list_cache(ct_id, obj_id, parent_id)

    if affected_voter_comment_pairs:
        get_user_vote_on_comment.dirty_multi(
            [(v, c) for v, c in affected_voter_comment_pairs]
        )

    return stats
