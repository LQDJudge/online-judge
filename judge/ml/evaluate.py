"""
Evaluate recommendation models using standard IR metrics.

Usage (from project root, with venv activated):
    python judge/ml/evaluate.py
    python judge/ml/evaluate.py --K 5 10 20 --sample-users 2000
    python judge/ml/evaluate.py --models collab_filter popular random

Methodology:
  - For each sampled user, randomly hold out N problems as ground truth
  - Rank all unseen candidate problems by model score
  - Measure how well the held-out items appear in top-K

Metrics:
  - Hit Rate@K: fraction of users with >= 1 hit in top-K
  - Precision@K: avg fraction of top-K that are relevant
  - Recall@K: avg fraction of relevant items found in top-K
  - NDCG@K: Normalized Discounted Cumulative Gain
  - MRR@K: Mean Reciprocal Rank of first hit
"""

import argparse
import json
import math
import os
import random
import sys
import time

import numpy as np

# Django setup
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dmoj.settings")

import django

django.setup()

from collections import defaultdict

from django.db import connection

from judge.ml.vector_store import TABLE_MAP


def load_embeddings_from_db(table, id_col):
    """Load all embeddings from a table into {id: numpy array}."""
    with connection.cursor() as c:
        c.execute(f"SELECT {id_col}, Vec_ToText(embedding) FROM {table}")
        rows = c.fetchall()
    return {int(row[0]): np.array(json.loads(row[1]), dtype=np.float32) for row in rows}


def build_user_problem_sets():
    """Build {user_id: set of problem_ids} from submissions table."""
    with connection.cursor() as c:
        c.execute(
            "SET STATEMENT max_statement_time=600 FOR "
            "SELECT user_id, problem_id FROM judge_submission "
            "GROUP BY user_id, problem_id"
        )
        rows = c.fetchall()
    user_problems = defaultdict(set)
    for uid, pid in rows:
        user_problems[uid].add(pid)
    return user_problems


def get_public_problem_ids():
    with connection.cursor() as c:
        c.execute(
            "SELECT id FROM judge_problem WHERE is_public = 1 AND is_organization_private = 0"
        )
        return {row[0] for row in c.fetchall()}


def get_popular_ranking(public_pids, limit=500):
    """Rank problems by number of distinct AC solvers."""
    placeholders = ",".join(["%s"] * len(public_pids))
    with connection.cursor() as c:
        c.execute(
            f"SET STATEMENT max_statement_time=600 FOR "
            f"SELECT problem_id, COUNT(DISTINCT user_id) as cnt "
            f"FROM judge_submission WHERE result = 'AC' AND problem_id IN ({placeholders}) "
            f"GROUP BY problem_id ORDER BY cnt DESC LIMIT %s",
            list(public_pids) + [limit],
        )
        return [row[0] for row in c.fetchall()]


def batch_score_cosine(user_embs, problem_embs, problem_ids):
    """
    Compute cosine similarity for all users against all problems at once.
    Returns (user_ids, scores_matrix) where scores_matrix[i] are scores for user i.
    """
    uids = list(user_embs.keys())
    U = np.stack([user_embs[uid] for uid in uids])  # (n_users, dim)
    V = np.stack([problem_embs[pid] for pid in problem_ids])  # (n_problems, dim)

    # Normalize
    U_norm = np.linalg.norm(U, axis=1, keepdims=True)
    U_norm[U_norm == 0] = 1
    U = U / U_norm

    V_norm = np.linalg.norm(V, axis=1, keepdims=True)
    V_norm[V_norm == 0] = 1
    V = V / V_norm

    # (n_users, n_problems) cosine similarities
    scores = U @ V.T
    return uids, scores


def compute_metrics(ranked_hits, K_values):
    """Given a list of 0/1 hits in ranked order, compute metrics for each K."""
    results = {}
    for K in K_values:
        hits = ranked_hits[:K]
        n_hits = sum(hits)
        n_relevant = sum(ranked_hits)  # total relevant items

        hit_rate = 1.0 if n_hits > 0 else 0.0
        precision = n_hits / K
        recall = n_hits / n_relevant if n_relevant > 0 else 0.0
        dcg = sum(hits[i] / math.log2(i + 2) for i in range(len(hits)))
        idcg = sum(1.0 / math.log2(i + 2) for i in range(min(n_relevant, K)))
        ndcg = dcg / idcg if idcg > 0 else 0.0
        mrr = 0.0
        for i, h in enumerate(hits):
            if h:
                mrr = 1.0 / (i + 1)
                break

        results[K] = {
            "hit_rate": hit_rate,
            "precision": precision,
            "recall": recall,
            "ndcg": ndcg,
            "mrr": mrr,
        }
    return results


def evaluate_model(
    model_name,
    eval_users,
    user_train,
    user_test,
    candidate_pids,
    user_embs,
    problem_embs,
    popular_ranking,
    K_values,
    rng,
):
    """Evaluate a single model. Returns {K: {metric: avg_value}}."""
    max_K = max(K_values)
    pid_list = list(candidate_pids)
    pid_to_idx = {pid: i for i, pid in enumerate(pid_list)}

    # Pre-compute all scores at once for embedding-based models
    scores_matrix = None
    uid_to_row = None
    has_embeddings = bool(user_embs and problem_embs)
    if has_embeddings:
        # Filter to problems that have embeddings in this model
        pid_list = [pid for pid in pid_list if pid in problem_embs]
        pid_to_idx = {pid: i for i, pid in enumerate(pid_list)}
        eval_user_embs = {uid: user_embs[uid] for uid in eval_users if uid in user_embs}
        if not eval_user_embs:
            return None
        uids, scores_matrix = batch_score_cosine(eval_user_embs, problem_embs, pid_list)
        uid_to_row = {uid: i for i, uid in enumerate(uids)}

    popular_set_ranking = [pid for pid in popular_ranking if pid in candidate_pids]

    agg = {K: defaultdict(float) for K in K_values}
    n = 0

    for uid in eval_users:
        train_set = user_train[uid]
        test_set = user_test[uid]

        if has_embeddings:
            if uid not in uid_to_row:
                continue
            row = uid_to_row[uid]
            user_scores = scores_matrix[row]
            # Mask out training problems by setting score to -inf
            masked = user_scores.copy()
            for pid in train_set:
                if pid in pid_to_idx:
                    masked[pid_to_idx[pid]] = -np.inf
            top_indices = np.argpartition(masked, -max_K)[-max_K:]
            top_indices = top_indices[np.argsort(masked[top_indices])[::-1]]
            rec_pids = [pid_list[i] for i in top_indices]

        elif model_name == "popular":
            rec_pids = [p for p in popular_set_ranking if p not in train_set][:max_K]

        elif model_name == "random":
            pool = [p for p in pid_list if p not in train_set]
            rec_pids = rng.sample(pool, min(max_K, len(pool)))

        else:
            continue

        hits = [1 if p in test_set else 0 for p in rec_pids]
        user_metrics = compute_metrics(hits, K_values)
        for K in K_values:
            for metric, val in user_metrics[K].items():
                agg[K][metric] += val
        n += 1

    # Average
    for K in K_values:
        for metric in agg[K]:
            agg[K][metric] /= max(n, 1)

    return agg, n


def main():
    parser = argparse.ArgumentParser(description="Evaluate recommendation models")
    parser.add_argument("--K", type=int, nargs="+", default=[5, 10, 20, 50])
    parser.add_argument("--holdout", type=int, default=5)
    parser.add_argument("--sample-users", type=int, default=1000)
    parser.add_argument("--min-submissions", type=int, default=20)
    parser.add_argument(
        "--models",
        type=str,
        nargs="+",
        default=[
            "collab_filter",
            "collab_filter_time",
            "two_tower",
            "popular",
            "random",
        ],
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    K_values = sorted(args.K)

    # Set generous query timeout for heavy evaluation queries
    with connection.cursor() as c:
        c.execute("SET SESSION max_statement_time=600")

    print("Loading data...")
    t0 = time.time()

    public_pids = get_public_problem_ids()
    print(f"  Public problems: {len(public_pids)}")

    user_problems = build_user_problem_sets()
    eligible = {
        uid: pids & public_pids
        for uid, pids in user_problems.items()
        if len(pids & public_pids) >= args.min_submissions
    }
    print(
        f"  Users with >= {args.min_submissions} submissions on public problems: {len(eligible)}"
    )

    # Load embeddings
    user_embs = {}
    problem_embs = {}
    for model in args.models:
        if model in TABLE_MAP:
            tables = TABLE_MAP[model]
            user_embs[model] = load_embeddings_from_db(tables["user"], "user_id")
            problem_embs[model] = load_embeddings_from_db(
                tables["problem"], "problem_id"
            )
            print(
                f"  Loaded {model}: {len(user_embs[model])} users, {len(problem_embs[model])} problems"
            )

    # Filter to users with embeddings
    has_embedding = set()
    for model in args.models:
        if model in user_embs:
            has_embedding |= set(user_embs[model].keys())
    if has_embedding:
        eligible = {uid: pids for uid, pids in eligible.items() if uid in has_embedding}
    print(f"  With embeddings: {len(eligible)}")

    # Sample users
    eval_user_ids = list(eligible.keys())
    if len(eval_user_ids) > args.sample_users:
        eval_user_ids = rng.sample(eval_user_ids, args.sample_users)
    print(f"  Sampled: {len(eval_user_ids)} users")

    # Split per user: random holdout
    user_train = {}
    user_test = {}
    for uid in eval_user_ids:
        all_pids = list(eligible[uid])
        if len(all_pids) < args.holdout + 5:
            continue
        test = set(rng.sample(all_pids, args.holdout))
        user_train[uid] = eligible[uid] - test
        user_test[uid] = test

    eval_users = list(user_train.keys())
    print(f"  Final eval users: {len(eval_users)}")
    print(f"  Setup: {time.time() - t0:.1f}s")

    # Problem candidate pool
    candidate_pids = set()
    for model in args.models:
        if model in problem_embs:
            candidate_pids |= set(problem_embs[model].keys()) & public_pids
    if not candidate_pids:
        candidate_pids = public_pids
    print(f"  Candidate problems: {len(candidate_pids)}")

    popular_ranking = get_popular_ranking(public_pids)

    # Evaluate each model
    all_results = {}
    for model in args.models:
        print(f"\nEvaluating: {model}")
        t0 = time.time()

        u_emb = user_embs.get(model, {})
        p_emb = problem_embs.get(model, {})

        result = evaluate_model(
            model,
            eval_users,
            user_train,
            user_test,
            candidate_pids,
            u_emb,
            p_emb,
            popular_ranking,
            K_values,
            rng,
        )
        if result is None:
            print("  Skipped (no embeddings)")
            continue
        agg, n = result
        all_results[model] = agg
        print(f"  {n} users, {time.time() - t0:.1f}s")
        print(
            f"  {'K':>4s}  {'HitRate':>8s}  {'Prec@K':>8s}  {'Recall@K':>8s}  {'NDCG@K':>8s}  {'MRR':>8s}"
        )
        print(f"  {'-'*52}")
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
        for model in all_results:
            header += f"  {model:>16s}"
        print(header)
        print(f"  {'-' * (4 + 18 * len(all_results))}")
        for K in K_values:
            row = f"  {K:4d}"
            for model in all_results:
                row += f"  {all_results[model][K]['ndcg']:16.4f}"
            print(row)


if __name__ == "__main__":
    main()
