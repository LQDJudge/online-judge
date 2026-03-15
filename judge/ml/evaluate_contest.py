"""
Evaluate contest recommendation strategies using standard IR metrics.

Usage (from project root, with venv activated):
    python judge/ml/evaluate_contest.py
    python judge/ml/evaluate_contest.py --K 5 10 20 --holdout 3
    python judge/ml/evaluate_contest.py --strategies difficulty embedding hybrid popular

Methodology:
  - For each sampled user, hold out most recent N contest participations as ground truth
  - Rank all eligible contests by strategy score
  - Measure how well held-out contests appear in top-K

Metrics:
  - Hit Rate@K: fraction of users with >= 1 hit in top-K
  - Precision@K: avg fraction of top-K that are relevant
  - Recall@K: avg fraction of relevant items found in top-K
  - NDCG@K: Normalized Discounted Cumulative Gain
  - MRR@K: Mean Reciprocal Rank of first hit
"""

import argparse
import math
import os
import random
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dmoj.settings")

import django

django.setup()

from collections import defaultdict

from django.db import connection

from judge.ml.evaluate import compute_metrics, load_embeddings_from_db
from judge.ml.vector_store import TABLE_MAP


def get_public_contest_ids():
    """Get IDs of public, non-org, non-course contests."""
    with connection.cursor() as c:
        c.execute(
            "SELECT id FROM judge_contest "
            "WHERE is_visible = 1 AND is_private = 0 "
            "AND is_organization_private = 0 AND is_in_course = 0"
        )
        return {row[0] for row in c.fetchall()}


def build_user_contest_participations():
    """
    Build {user_id: [(contest_id, start)]} sorted by start.
    Only real participations (virtual=0).
    """
    with connection.cursor() as c:
        c.execute(
            "SELECT user_id, contest_id, start "
            "FROM judge_contestparticipation "
            "WHERE virtual = 0 "
            "ORDER BY user_id, start"
        )
        rows = c.fetchall()
    user_contests = defaultdict(list)
    for uid, cid, start in rows:
        user_contests[uid].append((cid, start))
    return user_contests


def build_contest_problems():
    """Build {contest_id: [problem_id]}."""
    with connection.cursor() as c:
        c.execute(
            "SELECT contest_id, problem_id FROM judge_contestproblem "
            "WHERE problem_id IS NOT NULL"
        )
        rows = c.fetchall()
    contest_problems = defaultdict(list)
    for cid, pid in rows:
        contest_problems[cid].append(pid)
    return contest_problems


def build_user_solved_problems():
    """Build {user_id: set(problem_id)} for AC submissions."""
    with connection.cursor() as c:
        c.execute(
            "SET STATEMENT max_statement_time=600 FOR "
            "SELECT user_id, problem_id FROM judge_submission "
            "WHERE result = 'AC' "
            "GROUP BY user_id, problem_id"
        )
        rows = c.fetchall()
    user_solved = defaultdict(set)
    for uid, pid in rows:
        user_solved[uid].add(pid)
    return user_solved


def get_problem_points():
    """Build {problem_id: points}."""
    with connection.cursor() as c:
        c.execute("SELECT id, points FROM judge_problem WHERE points IS NOT NULL")
        return {row[0]: float(row[1]) for row in c.fetchall()}


def get_contest_user_counts(contest_ids):
    """Build {contest_id: user_count} for popularity ranking."""
    if not contest_ids:
        return {}
    placeholders = ",".join(["%s"] * len(contest_ids))
    with connection.cursor() as c:
        c.execute(
            f"SELECT id, user_count FROM judge_contest WHERE id IN ({placeholders})",
            list(contest_ids),
        )
        return {row[0]: row[1] for row in c.fetchall()}


def score_difficulty(user_skill, contest_difficulty):
    """Gaussian difficulty match centered on user skill."""
    if user_skill is None or contest_difficulty is None:
        return 0.0
    sigma = max(200, user_skill * 0.3)
    return math.exp(-((user_skill - contest_difficulty) ** 2) / (2 * sigma**2))


def score_embedding(user_vec, contest_problems, problem_embs, solved_pids):
    """Score a contest by embedding similarity."""
    unsolved = [pid for pid in contest_problems if pid not in solved_pids]
    if user_vec is None or not unsolved:
        return 0.0
    scores = []
    for pid in unsolved:
        if pid in problem_embs:
            p_vec = problem_embs[pid]
            p_norm = np.linalg.norm(p_vec)
            if p_norm > 0:
                scores.append(float(np.dot(user_vec, p_vec / p_norm)))
    return sum(scores) / len(scores) if scores else 0.0


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate contest recommendation strategies"
    )
    parser.add_argument("--K", type=int, nargs="+", default=[5, 10, 20])
    parser.add_argument("--holdout", type=int, default=3)
    parser.add_argument("--sample-users", type=int, default=500)
    parser.add_argument("--min-participations", type=int, default=5)
    parser.add_argument(
        "--strategies",
        type=str,
        nargs="+",
        default=["production", "difficulty", "embedding", "popular"],
    )
    parser.add_argument(
        "--alpha", type=float, default=0.3, help="Weight for difficulty in hybrid"
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    K_values = sorted(args.K)

    with connection.cursor() as c:
        c.execute("SET SESSION max_statement_time=600")

    print("Loading data...")
    t0 = time.time()

    public_cids = get_public_contest_ids()
    print(f"  Public contests: {len(public_cids)}")

    user_contests = build_user_contest_participations()
    # Filter to public contests only
    user_contests = {
        uid: [(cid, t) for cid, t in parts if cid in public_cids]
        for uid, parts in user_contests.items()
    }
    eligible = {
        uid: parts
        for uid, parts in user_contests.items()
        if len(parts) >= args.min_participations
    }
    print(f"  Users with >= {args.min_participations} participations: {len(eligible)}")

    contest_problems = build_contest_problems()
    user_solved = build_user_solved_problems()
    problem_points = get_problem_points()

    # Load embeddings
    user_embs = {}
    problem_embs = {}
    if any(s in ("embedding", "hybrid") for s in args.strategies):
        tables = TABLE_MAP["two_tower"]
        raw_user = load_embeddings_from_db(tables["user"], "user_id")
        raw_prob = load_embeddings_from_db(tables["problem"], "problem_id")
        # Normalize user embeddings
        for uid, vec in raw_user.items():
            norm = np.linalg.norm(vec)
            user_embs[uid] = vec / norm if norm > 0 else vec
        problem_embs = raw_prob
        print(
            f"  Loaded embeddings: {len(user_embs)} users, {len(problem_embs)} problems"
        )

    # Sample users
    eval_user_ids = list(eligible.keys())
    if user_embs:
        eval_user_ids = [uid for uid in eval_user_ids if uid in user_embs]
    if len(eval_user_ids) > args.sample_users:
        eval_user_ids = rng.sample(eval_user_ids, args.sample_users)
    print(f"  Sampled: {len(eval_user_ids)} users")

    # Temporal split: hold out most recent N participations
    user_train = {}
    user_test = {}
    for uid in eval_user_ids:
        parts = eligible[uid]  # already sorted by real_start
        if len(parts) < args.holdout + 2:
            continue
        test_cids = set(cid for cid, _ in parts[-args.holdout :])
        train_cids = set(cid for cid, _ in parts[: -args.holdout])
        user_train[uid] = train_cids
        user_test[uid] = test_cids

    eval_users = list(user_train.keys())
    print(f"  Final eval users: {len(eval_users)}")

    # Compute user skill (p75 of solved problem points, or rating if available)
    user_skill = {}
    user_ratings = {}
    with connection.cursor() as c:
        c.execute("SELECT id, rating FROM judge_profile WHERE rating IS NOT NULL")
        user_ratings = {row[0]: row[1] for row in c.fetchall()}
    for uid in eval_users:
        solved = user_solved.get(uid, set())
        pts = sorted([problem_points[pid] for pid in solved if pid in problem_points])
        p75 = pts[int(len(pts) * 0.75)] if len(pts) >= 4 else (pts[-1] if pts else None)
        rating = user_ratings.get(uid)
        if rating and p75:
            user_skill[uid] = max(float(rating), p75)
        elif rating:
            user_skill[uid] = float(rating)
        else:
            user_skill[uid] = p75

    # Contest difficulty (avg points of ALL problems, not just unsolved)
    contest_difficulty = {}
    for cid in public_cids:
        pids = contest_problems.get(cid, [])
        pts = [problem_points[pid] for pid in pids if pid in problem_points]
        contest_difficulty[cid] = sum(pts) / len(pts) if pts else None

    # Popularity ranking by user_count (matches production fallback)
    contest_user_counts = get_contest_user_counts(public_cids)
    max_users = max(contest_user_counts.values()) if contest_user_counts else 1
    popular_ranking = sorted(
        public_cids,
        key=lambda cid: contest_user_counts.get(cid, 0),
        reverse=True,
    )

    print(f"  Setup: {time.time() - t0:.1f}s")

    # Evaluate each strategy
    max_K = max(K_values)
    all_results = {}

    for strategy in args.strategies:
        print(f"\nEvaluating: {strategy}")
        t0 = time.time()
        agg = {K: defaultdict(float) for K in K_values}
        n = 0

        for uid in eval_users:
            train_cids = user_train[uid]
            test_cids = user_test[uid]
            solved = user_solved.get(uid, set())

            # Score all public contests not in training set, with min users filter
            candidates = [
                cid
                for cid in public_cids
                if cid not in train_cids and contest_user_counts.get(cid, 0) >= 5
            ]

            if strategy == "difficulty":
                scored = [
                    (
                        cid,
                        score_difficulty(user_skill[uid], contest_difficulty.get(cid)),
                    )
                    for cid in candidates
                ]
            elif strategy == "embedding":
                u_vec = user_embs.get(uid)
                scored = [
                    (
                        cid,
                        score_embedding(
                            u_vec,
                            contest_problems.get(cid, []),
                            problem_embs,
                            solved,
                        ),
                    )
                    for cid in candidates
                ]
            elif strategy == "production":
                # Matches production scoring: pop 0.4, diff 0.35, emb 0.25
                u_vec = user_embs.get(uid)
                scored = []
                for cid in candidates:
                    d = score_difficulty(user_skill[uid], contest_difficulty.get(cid))
                    e = score_embedding(
                        u_vec, contest_problems.get(cid, []), problem_embs, solved
                    )
                    e_norm = (e + 1.0) / 2.0
                    uc = contest_user_counts.get(cid, 0)
                    pop = math.log1p(uc) / math.log1p(max_users) if max_users > 0 else 0
                    scored.append((cid, 0.4 * pop + 0.35 * d + 0.25 * e_norm))
            elif strategy == "popular":
                scored = [
                    (cid, -i)
                    for i, cid in enumerate(popular_ranking)
                    if cid not in train_cids and contest_user_counts.get(cid, 0) >= 5
                ]
            else:
                continue

            scored.sort(key=lambda x: -x[1])
            rec_cids = [cid for cid, _ in scored[:max_K]]
            hits = [1 if cid in test_cids else 0 for cid in rec_cids]
            user_metrics = compute_metrics(hits, K_values)
            for K in K_values:
                for metric, val in user_metrics[K].items():
                    agg[K][metric] += val
            n += 1

        for K in K_values:
            for metric in agg[K]:
                agg[K][metric] /= max(n, 1)

        all_results[strategy] = agg
        print(f"  {n} users, {time.time() - t0:.1f}s")
        print(
            f"  {'K':>4s}  {'HitRate':>8s}  {'Prec@K':>8s}  {'Recall@K':>8s}  {'NDCG@K':>8s}  {'MRR':>8s}"
        )
        print(f"  {'-' * 52}")
        for K in K_values:
            m = agg[K]
            print(
                f"  {K:4d}  {m['hit_rate']:8.4f}  {m['precision']:8.4f}  "
                f"{m['recall']:8.4f}  {m['ndcg']:8.4f}  {m['mrr']:8.4f}"
            )

    # Summary comparison
    if len(all_results) > 1:
        print("\n" + "=" * 70)
        print("COMPARISON (NDCG@K)")
        print("=" * 70)
        header = f"  {'K':>4s}"
        for strategy in all_results:
            header += f"  {strategy:>16s}"
        print(header)
        print(f"  {'-' * (4 + 18 * len(all_results))}")
        for K in K_values:
            row = f"  {K:4d}"
            for strategy in all_results:
                row += f"  {all_results[strategy][K]['ndcg']:16.4f}"
            print(row)


if __name__ == "__main__":
    main()
