from collections import defaultdict

from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Sum, Q
from django.utils import timezone

from judge.models import BlogPost, Contest, Problem, Profile, Solution, Submission
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


# --- Trust weighting -------------------------------------------------------
# Each vote's impact on contribution is scaled by the voter's trust weight in
# [0, 1]. Discount-only (capped at 1.0): throwaway / low-activity accounts
# approach 0 so sockpuppet brigades carry almost no weight, while established
# users sit at 1.0 (no amplification, no oligarchy effect). Trust is the max of
# three normalized signals — solved-count, points, rating — so a single strong
# signal grants full trust and legitimate *unrated* solvers are not penalized.
# Account age is intentionally NOT used. Applied only in the daily recompute;
# the live per-vote delta stays unweighted and is reconciled on the next run.
TRUST_FULL_SOLVED = 50
TRUST_FULL_POINTS = 50000
TRUST_RATING_FLOOR = 1000
TRUST_RATING_CEIL = 1700


def trust_weight(problem_count, points, rating):
    """Trust weight in [0, 1] from a voter's solved-count, points, and rating.

    The maximum of three normalized signals (any single strong signal grants
    full trust), clamped to [0, 1]. Rating is optional — unrated voters simply
    fall back to the solved/points signals rather than being penalized.
    """
    s_solved = (problem_count or 0) / TRUST_FULL_SOLVED
    s_points = float(points or 0) / TRUST_FULL_POINTS
    if rating:
        s_rating = (rating - TRUST_RATING_FLOOR) / (
            TRUST_RATING_CEIL - TRUST_RATING_FLOOR
        )
    else:
        s_rating = 0.0
    return max(0.0, min(1.0, max(s_solved, s_points, s_rating)))


def _trust_weights_for(voter_ids):
    """{voter_id: trust_weight} for the given voter ids (one batched query)."""
    return {
        vid: trust_weight(pc, pts, rating)
        for vid, pc, pts, rating in Profile.objects.filter(
            id__in=voter_ids
        ).values_list("id", "problem_count", "points", "rating")
    }


def compute_contribution(profile):
    """
    Compute global contribution points for a profile.

    contribution_points = trust-weighted sum of votes across all public content
    authored by the user (PageVote voter rows) plus the trust-weighted sum of
    votes on comments the user authored on any public/community content.
    Each vote is scaled by its voter's trust weight; the total is rounded to an
    integer before it is stored.
    """
    pv_rows = []  # (voter_id, score) on the user's authored content
    for ct, ids in _get_public_content_ids_by_author(profile).items():
        pagevote_ids = list(
            PageVote.objects.filter(content_type=ct, object_id__in=ids).values_list(
                "id", flat=True
            )
        )
        if pagevote_ids:
            pv_rows += list(
                PageVoteVoter.objects.filter(pagevote_id__in=pagevote_ids).values_list(
                    "voter_id", "score"
                )
            )

    cm_rows = []  # (voter_id, score) on the user's comments
    for ct, ids in _get_all_public_content_ids().items():
        comment_ids = list(
            Comment.objects.filter(
                content_type=ct,
                object_id__in=ids,
                author=profile,
                hidden=False,
            ).values_list("id", flat=True)
        )
        if comment_ids:
            cm_rows += list(
                CommentVote.objects.filter(comment_id__in=comment_ids).values_list(
                    "voter_id", "score"
                )
            )

    weights = _trust_weights_for({v for v, _ in pv_rows} | {v for v, _ in cm_rows})
    total = sum(weights.get(v, 0.0) * s for v, s in pv_rows) + sum(
        weights.get(v, 0.0) * s for v, s in cm_rows
    )
    return round(total)


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
    Compute trust-weighted contribution_points for ALL profiles in bulk.
    Returns dict of {profile_id: contribution_points} (rounded, clamped to the
    IntegerField range). Much faster than calling compute_contribution() per
    profile. Consistent with compute_contribution: each vote is scaled by its
    voter's trust weight.
    """
    community_org_ids = list(
        Organization.objects.filter(is_community=True).values_list("id", flat=True)
    )

    # (author_id, voter_id, score) contribution edges across all public content.
    edges = []
    voter_ids = set()

    for model, public_filter in _PUBLIC_CONTENT_CONFIGS:
        ct = ContentType.objects.get_for_model(model)
        public_ids = _public_ids_for(model, public_filter, community_org_ids)
        if not public_ids:
            continue

        # 1. PageVote voter rows -> credit to content authors.
        pv_obj = dict(
            PageVote.objects.filter(
                content_type=ct, object_id__in=public_ids
            ).values_list("id", "object_id")
        )
        if pv_obj:
            lname = model.__name__.lower()
            obj2auth = defaultdict(list)
            for obj_id, author_id in model.authors.through.objects.filter(
                **{f"{lname}_id__in": set(pv_obj.values())}
            ).values_list(f"{lname}_id", "profile_id"):
                obj2auth[obj_id].append(author_id)
            for voter_id, pvid, score in PageVoteVoter.objects.filter(
                pagevote_id__in=pv_obj.keys()
            ).values_list("voter_id", "pagevote_id", "score"):
                for author_id in obj2auth.get(pv_obj[pvid], ()):
                    edges.append((author_id, voter_id, score))
                    voter_ids.add(voter_id)

        # 2. Comment voter rows -> credit to comment authors.
        comm_author = dict(
            Comment.objects.filter(
                content_type=ct, object_id__in=public_ids, hidden=False
            )
            .exclude(author=None)
            .values_list("id", "author_id")
        )
        if comm_author:
            for voter_id, cid, score in CommentVote.objects.filter(
                comment_id__in=comm_author.keys()
            ).values_list("voter_id", "comment_id", "score"):
                edges.append((comm_author[cid], voter_id, score))
                voter_ids.add(voter_id)

    weights = _trust_weights_for(voter_ids)

    raw = defaultdict(float)
    for author_id, voter_id, score in edges:
        raw[author_id] += weights.get(voter_id, 0.0) * score

    # Round to int and clamp to IntegerField range.
    INT_MAX = 2147483647
    INT_MIN = -2147483648
    return {
        author_id: max(INT_MIN, min(INT_MAX, round(val)))
        for author_id, val in raw.items()
    }


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
    if not flagged_voter_ids:
        return _empty_purge_stats()

    pv_rows = PageVoteVoter.objects.filter(voter_id__in=flagged_voter_ids, score=-1)
    cv_rows = CommentVote.objects.filter(voter_id__in=flagged_voter_ids, score=-1)
    return _purge_downvote_rows(pv_rows, cv_rows)


def _empty_purge_stats():
    return {
        "pagevote_deleted": 0,
        "commentvote_deleted": 0,
        "pagevotes_rescored": 0,
        "comments_rescored": 0,
    }


def _purge_downvote_rows(pv_rows, cv_rows):
    """
    Delete the given score=-1 PageVoteVoter / CommentVote rows, re-sum the
    affected PageVote.score / Comment.score, and dirty the same caches the
    normal vote path dirties. Shared by purge_downvotes_from (global, by voter)
    and purge_brigade_downvotes (surgical, by voter+author). Returns a stats
    dict. The caller dirties get_contribution_rank and recomputes
    Profile.contribution_points.
    """
    stats = _empty_purge_stats()

    # Snapshot what we're about to touch before deletion.
    affected_pv_ids = set(pv_rows.values_list("pagevote_id", flat=True))
    affected_voter_comment_pairs = list(cv_rows.values_list("voter_id", "comment_id"))
    affected_comment_ids = {c for _, c in affected_voter_comment_pairs}

    parent_triples = set()
    if affected_comment_ids:
        for ct_id, obj_id, parent_id in Comment.objects.filter(
            id__in=affected_comment_ids
        ).values_list("content_type_id", "object_id", "parent_id"):
            parent_triples.add((ct_id, obj_id, parent_id))

    # Delete the -1 rows.
    stats["pagevote_deleted"], _ = pv_rows.delete()
    stats["commentvote_deleted"], _ = cv_rows.delete()

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


# Content configs shared by the contribution + brigade queries: (model, public filter).
_PUBLIC_CONTENT_CONFIGS = [
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


def _public_ids_for(model, public_filter, community_org_ids):
    """Ids of a model's public content (including community-org blogs)."""
    ids = set(model.objects.filter(public_filter).values_list("id", flat=True))
    if model == BlogPost and community_org_ids:
        ids |= set(
            BlogPost.objects.filter(
                visible=True,
                is_organization_private=True,
                organizations__in=community_org_ids,
            )
            .distinct()
            .values_list("id", flat=True)
        )
    return ids


def detect_targeted_downvote_brigades(
    min_downvotes_on_author=5,
    min_concentration=0.6,
    min_covote_jaccard=0.6,
    fresh_days=90,
    fresh_max_submissions=10,
):
    """
    Detect coordinated downvote brigades targeting a single author's public
    content. Complements ``detect_abusive_downvoters`` (which is per-voter and
    global) by looking at the (voter, author) axis: a voter who dumps downvotes
    onto one author is flagged when concentration/coordination signals fire,
    even if the voter's *global* downvote volume is below that detector's gate.

    "Downvotes against author X" = PageVoteVoter(score=-1) on X's authored
    public content + CommentVote(score=-1) on comments X authored on public
    content.

    A (voter, author) pair is flagged when the voter cast at least
    ``min_downvotes_on_author`` such downvotes AND at least one signal fires:
      - "concentration": those downvotes are >= ``min_concentration`` of the
        voter's total downvotes site-wide (they mostly exist to hit this author).
      - "covote":        the voter's downvote target set overlaps another gated
        voter of the same author with Jaccard >= ``min_covote_jaccard`` (lockstep).
      - "ip_shared":     the voter shares a last-IP with another gated voter of
        the same author (sockpuppets / one operator).
      - "fresh_low":     account age < ``fresh_days`` days AND < ``fresh_max_submissions``
        submissions (throwaway account whose main activity is downvoting).

    Returns ``{author_id: {voter_id: {"here", "concentration", "covote",
    "ip_shared", "fresh_low", "signals": [...]}}}`` for authors with >= 1
    flagged voter.
    """
    community_org_ids = list(
        Organization.objects.filter(is_community=True).values_list("id", flat=True)
    )

    # (voter_id, author_id) -> set of downvoted target keys ("pv"/"cv", id)
    va_targets = defaultdict(set)
    # author_id -> set of voter_ids that downvoted them
    author_voters = defaultdict(set)

    for model, public_filter in _PUBLIC_CONTENT_CONFIGS:
        ct = ContentType.objects.get_for_model(model)
        public_ids = _public_ids_for(model, public_filter, community_org_ids)
        if not public_ids:
            continue

        # --- PageVote downvotes on the author's content ---
        pv_obj = dict(
            PageVote.objects.filter(
                content_type=ct, object_id__in=public_ids
            ).values_list("id", "object_id")
        )
        if pv_obj:
            lname = model.__name__.lower()
            obj2auth = defaultdict(list)
            for obj_id, prof_id in model.authors.through.objects.filter(
                **{f"{lname}_id__in": set(pv_obj.values())}
            ).values_list(f"{lname}_id", "profile_id"):
                obj2auth[obj_id].append(prof_id)
            for voter_id, pvid in PageVoteVoter.objects.filter(
                pagevote_id__in=pv_obj.keys(), score=-1
            ).values_list("voter_id", "pagevote_id"):
                for author_id in obj2auth.get(pv_obj[pvid], ()):
                    if author_id == voter_id:
                        continue
                    va_targets[(voter_id, author_id)].add(("pv", pvid))
                    author_voters[author_id].add(voter_id)

        # --- Comment downvotes on comments the author wrote on this content ---
        comm_author = dict(
            Comment.objects.filter(
                content_type=ct, object_id__in=public_ids, hidden=False
            )
            .exclude(author=None)
            .values_list("id", "author_id")
        )
        if comm_author:
            for voter_id, cid in CommentVote.objects.filter(
                comment_id__in=comm_author.keys(), score=-1
            ).values_list("voter_id", "comment_id"):
                author_id = comm_author.get(cid)
                if author_id is None or author_id == voter_id:
                    continue
                va_targets[(voter_id, author_id)].add(("cv", cid))
                author_voters[author_id].add(voter_id)

    if not va_targets:
        return {}

    voter_ids = {v for (v, _a) in va_targets}

    # Global downvote count per voter (denominator for concentration).
    global_down = defaultdict(int)
    for table in (PageVoteVoter, CommentVote):
        for row in (
            table.objects.filter(voter_id__in=voter_ids, score=-1)
            .values("voter_id")
            .annotate(n=Count("id"))
        ):
            global_down[row["voter_id"]] += row["n"]

    # Voter profile info: last-IP, account age, submission count.
    now = timezone.now()
    profiles = {
        p.id: p for p in Profile.objects.filter(id__in=voter_ids).select_related("user")
    }
    subs = defaultdict(int)
    for row in (
        Submission.objects.filter(user_id__in=voter_ids)
        .values("user_id")
        .annotate(n=Count("id"))
    ):
        subs[row["user_id"]] = row["n"]

    def is_fresh_low(vid):
        p = profiles.get(vid)
        if not p:
            return False
        age_days = (now - p.user.date_joined).days
        return age_days < fresh_days and subs.get(vid, 0) < fresh_max_submissions

    result = {}
    for author_id, voters in author_voters.items():
        # Only voters above the per-author volume gate participate.
        gated = {
            v
            for v in voters
            if len(va_targets[(v, author_id)]) >= min_downvotes_on_author
        }
        if not gated:
            continue
        ip_of = {v: (profiles[v].ip if v in profiles else None) for v in gated}

        flagged = {}
        for v in gated:
            here_set = va_targets[(v, author_id)]
            here = len(here_set)
            gd = global_down.get(v) or here
            concentration = here / gd if gd else 0.0

            covote = 0.0
            for o in gated:
                if o == v:
                    continue
                other = va_targets[(o, author_id)]
                union = len(here_set | other)
                if union:
                    covote = max(covote, len(here_set & other) / union)

            ip_shared = bool(ip_of[v]) and any(
                o != v and ip_of.get(o) == ip_of[v] for o in gated
            )
            fresh_low = is_fresh_low(v)

            signals = []
            if concentration >= min_concentration:
                signals.append("concentration")
            if covote >= min_covote_jaccard:
                signals.append("covote")
            if ip_shared:
                signals.append("ip_shared")
            if fresh_low:
                signals.append("fresh_low")

            if signals:
                flagged[v] = {
                    "here": here,
                    "concentration": round(concentration, 3),
                    "covote": round(covote, 3),
                    "ip_shared": ip_shared,
                    "fresh_low": fresh_low,
                    "signals": signals,
                }
        if flagged:
            result[author_id] = flagged
    return result


def purge_brigade_downvotes(brigades):
    """
    Surgically purge a detected brigade: for each flagged (author, voter) pair,
    delete that voter's score=-1 votes ONLY against that author's public content
    (their votes on other authors are left untouched). `brigades` is the dict
    returned by detect_targeted_downvote_brigades. Returns the same stats dict
    as purge_downvotes_from. The caller recomputes affected authors'
    contribution_points and dirties get_contribution_rank.
    """
    if not brigades:
        return _empty_purge_stats()

    all_public = _get_all_public_content_ids()
    pv_row_ids = set()
    cv_row_ids = set()

    for author_id, voters in brigades.items():
        voter_ids = list(voters.keys())

        # This author's authored public content -> their pagevotes.
        author_pagevote_ids = []
        for ct, ids in _get_public_content_ids_by_author(Profile(id=author_id)).items():
            author_pagevote_ids += list(
                PageVote.objects.filter(content_type=ct, object_id__in=ids).values_list(
                    "id", flat=True
                )
            )
        if author_pagevote_ids:
            pv_row_ids |= set(
                PageVoteVoter.objects.filter(
                    voter_id__in=voter_ids,
                    score=-1,
                    pagevote_id__in=author_pagevote_ids,
                ).values_list("id", flat=True)
            )

        # This author's comments on public content.
        author_comment_ids = []
        for ct, ids in all_public.items():
            author_comment_ids += list(
                Comment.objects.filter(
                    content_type=ct,
                    object_id__in=ids,
                    author_id=author_id,
                    hidden=False,
                ).values_list("id", flat=True)
            )
        if author_comment_ids:
            cv_row_ids |= set(
                CommentVote.objects.filter(
                    voter_id__in=voter_ids,
                    score=-1,
                    comment_id__in=author_comment_ids,
                ).values_list("id", flat=True)
            )

    pv_rows = PageVoteVoter.objects.filter(id__in=pv_row_ids)
    cv_rows = CommentVote.objects.filter(id__in=cv_row_ids)
    return _purge_downvote_rows(pv_rows, cv_rows)
