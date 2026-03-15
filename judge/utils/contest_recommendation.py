import math
import random
from datetime import datetime

from django.db import connection

from judge.caching import cache_wrapper
from judge.ml.vector_store import TABLE_MAP, _get_embedding
from judge.models import Contest, ContestParticipation, ContestProblem, Problem
from judge.utils.problems import user_completed_ids

MIN_CONTEST_USERS = 5


@cache_wrapper(prefix="public_contests", timeout=3600)
def get_public_contests():
    """
    Return IDs of all publicly visible contests.
    Excludes private, org-private, and course contests.
    Globally cacheable (same for all users).
    """
    return set(
        Contest.objects.filter(
            is_visible=True,
            is_private=False,
            is_organization_private=False,
            is_in_course=False,
        ).values_list("id", flat=True)
    )


@cache_wrapper(prefix="prob_contests", timeout=3600)
def get_contests_for_problem(problem_id):
    """
    Return list of public contests containing this problem,
    sorted by start_time ascending (earliest/original first).
    Returns list of dicts with id, key, name, start_time.
    """
    public_ids = get_public_contests()
    contests = (
        ContestProblem.objects.filter(
            problem_id=problem_id,
            contest_id__in=public_ids,
        )
        .select_related("contest")
        .order_by("contest__start_time")
        .values_list(
            "contest__id", "contest__key", "contest__name", "contest__start_time"
        )
    )
    return [
        {"id": c[0], "key": c[1], "name": c[2], "start_time": c[3]} for c in contests
    ]


@cache_wrapper(prefix="user_skill")
def _get_user_skill(profile):
    """
    Estimate user skill level.
    Uses max(rating, p75 of solved problem points) so strong users
    who also solved easy problems aren't dragged down by the average.
    Returns float or None.
    """
    completed_ids = user_completed_ids(profile)
    if not completed_ids:
        return float(profile.rating) if profile.rating else None

    solved_points = list(
        Problem.objects.filter(id__in=completed_ids, points__isnull=False).values_list(
            "points", flat=True
        )
    )
    if not solved_points:
        return float(profile.rating) if profile.rating else None

    pts = sorted([float(p) for p in solved_points])
    p75 = pts[int(len(pts) * 0.75)] if len(pts) >= 4 else pts[-1]

    if profile.rating:
        return max(float(profile.rating), p75)
    return p75


# --- Global cached data (same for all users) ---


@cache_wrapper(prefix="eligible_contest_uc", timeout=3600)
def _get_eligible_contest_user_counts():
    """
    Return {contest_id: user_count} for public contests with enough participants.
    Dirty on Contest save/delete.
    """
    public_ids = get_public_contests()
    return dict(
        Contest.objects.filter(
            id__in=public_ids, user_count__gte=MIN_CONTEST_USERS
        ).values_list("id", "user_count")
    )


@cache_wrapper(prefix="contest_prob_map", timeout=3600)
def _get_contest_problems_map():
    """
    Return {contest_id: [problem_id]} for all eligible public contests.
    Dirty on ContestProblem save/delete.
    """
    eligible_ids = set(_get_eligible_contest_user_counts().keys())
    result = {}
    for cid, pid in ContestProblem.objects.filter(
        contest_id__in=eligible_ids, problem__isnull=False
    ).values_list("contest_id", "problem_id"):
        result.setdefault(cid, []).append(pid)
    return result


@cache_wrapper(prefix="prob_points_map", timeout=3600)
def _get_problem_points_map():
    """Load all problem points in one query. Returns {problem_id: float}."""
    return {
        pid: float(pts)
        for pid, pts in Problem.objects.filter(points__isnull=False).values_list(
            "id", "points"
        )
    }


@cache_wrapper(prefix="contest_diff_map", timeout=3600)
def _get_contest_difficulty_map():
    """
    Compute avg problem points per contest.
    Returns {contest_id: avg_points}. Dirty on ContestProblem or Problem change.
    """
    contest_problems = _get_contest_problems_map()
    problem_points = _get_problem_points_map()
    result = {}
    for cid, pids in contest_problems.items():
        pts = [problem_points[pid] for pid in pids if pid in problem_points]
        result[cid] = sum(pts) / len(pts) if pts else None
    return result


# --- Per-user cached data ---


@cache_wrapper(prefix="user_participated")
def _get_participated_contest_ids(profile):
    """
    Return set of contest IDs the user has participated in (non-virtual).
    Dirty on ContestParticipation save/delete.
    """
    return set(
        ContestParticipation.objects.filter(user=profile, virtual=0).values_list(
            "contest_id", flat=True
        )
    )


# --- Scoring functions ---


def _score_difficulty_match(user_skill, contest_difficulty):
    """
    Gaussian-shaped score centered on user skill.
    sigma scales with skill level so higher-skilled users tolerate wider range.
    """
    if user_skill is None or contest_difficulty is None:
        return 0.0
    sigma = max(200, user_skill * 0.3)
    return math.exp(-((user_skill - contest_difficulty) ** 2) / (2 * sigma**2))


def _get_user_problem_similarities(user_id, problem_ids):
    """
    Ask DB to compute cosine similarity between user embedding and
    each problem embedding in one query. Returns {problem_id: similarity}.
    """
    if not problem_ids:
        return {}

    user_table = TABLE_MAP["two_tower"]["user"]
    user_vec = _get_embedding(user_table, "user_id", user_id, fallback_id=0)
    if not user_vec:
        return {}

    prob_table = TABLE_MAP["two_tower"]["problem"]
    placeholders = ",".join(["%s"] * len(problem_ids))
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT problem_id, "
            f"(1 - VEC_DISTANCE_COSINE(embedding, Vec_FromText(%s))) AS sim "
            f"FROM {prob_table} "
            f"WHERE problem_id IN ({placeholders})",
            [user_vec] + list(problem_ids),
        )
        return {row[0]: float(row[1]) for row in cursor.fetchall()}


def _score_embedding_similarity(problem_similarities, unsolved_problem_ids):
    """
    Mean cosine similarity for unsolved problems in a contest.
    problem_similarities is a pre-computed {pid: similarity} dict.
    """
    if not unsolved_problem_ids:
        return 0.0
    scores = [
        problem_similarities[pid]
        for pid in unsolved_problem_ids
        if pid in problem_similarities
    ]
    if not scores:
        return 0.0
    return sum(scores) / len(scores)


# --- Main recommendation function ---


@cache_wrapper(prefix="rec_contests", timeout=3600)
def get_recommended_contests(profile, limit=50):
    """
    Return list of (contest_id, score) tuples for recommended contests,
    sorted by score descending. Shuffled daily for freshness.

    Scoring: weighted combination of:
      - popularity (log-scaled user_count) — strongest signal
      - difficulty match (Gaussian centered on user skill)
      - embedding similarity (cosine sim from two-tower model)
    """
    contest_user_counts = _get_eligible_contest_user_counts()
    if not contest_user_counts:
        return []

    eligible_ids = set(contest_user_counts.keys())

    # Exclude contests the user has already participated in
    participated_ids = _get_participated_contest_ids(profile)
    eligible_ids -= participated_ids

    solved_ids = user_completed_ids(profile)
    user_skill = _get_user_skill(profile)

    # Get contest-problem mappings from cache
    all_contest_problems = _get_contest_problems_map()
    contest_problems = {
        cid: pids for cid, pids in all_contest_problems.items() if cid in eligible_ids
    }

    # Filter to contests with unsolved problems
    contest_unsolved = {}
    for cid, pids in contest_problems.items():
        unsolved = [pid for pid in pids if pid not in solved_ids]
        if unsolved:
            contest_unsolved[cid] = unsolved

    if not contest_unsolved:
        return []

    # Get cached global data
    contest_diff = _get_contest_difficulty_map()

    # Compute all problem similarities in one DB query (per-user, not cached separately)
    all_contest_pids = set()
    for pids in contest_problems.values():
        all_contest_pids.update(pids)
    problem_sims = _get_user_problem_similarities(profile.user_id, all_contest_pids)

    # Compute max user_count for popularity normalization
    max_users = max(contest_user_counts.values()) if contest_user_counts else 1

    # Score each contest (no DB queries in loop)
    scored = []
    for cid, unsolved_pids in contest_unsolved.items():
        # Difficulty: Gaussian match (0 to 1)
        diff_score = _score_difficulty_match(user_skill, contest_diff.get(cid))

        # Embedding: cosine similarity (-1 to 1) -> normalized to (0, 1)
        emb_score = _score_embedding_similarity(problem_sims, unsolved_pids)
        emb_score_norm = (emb_score + 1.0) / 2.0

        # Popularity: log-scaled (0 to 1)
        uc = contest_user_counts.get(cid, 0)
        pop_score = math.log1p(uc) / math.log1p(max_users) if max_users > 0 else 0

        # Weighted combination: popularity 0.4, difficulty 0.35, embedding 0.25
        score = 0.4 * pop_score + 0.35 * diff_score + 0.25 * emb_score_norm
        scored.append((cid, score))

    # Weighted shuffle: higher-scored contests are more likely to appear
    # near the top, but there's still daily variety.
    # Uses score as sampling weight — pick without replacement.
    seed = datetime.now().strftime("%d%m%Y")
    rng = random.Random(seed)
    result = []
    remaining = list(scored)
    while remaining and len(result) < limit:
        weights = [s**3 for _, s in remaining]  # cube to amplify differences
        total = sum(weights)
        if total == 0:
            break
        r = rng.random() * total
        cumulative = 0
        for i, (cid, s) in enumerate(remaining):
            cumulative += weights[i]
            if cumulative >= r:
                result.append((cid, s))
                remaining.pop(i)
                break

    return result


def get_recommended_contests_for_anonymous(limit=50):
    """Fallback: rank by user_count for anonymous users."""
    contest_user_counts = _get_eligible_contest_user_counts()
    if not contest_user_counts:
        return []
    sorted_ids = sorted(contest_user_counts, key=contest_user_counts.get, reverse=True)
    return sorted_ids[:limit]
