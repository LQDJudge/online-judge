"""
Dataset and feature preprocessing for Two Tower model.

All features are stored as dense tensors indexed by ID for O(1) batch construction.
Problem types use padded fixed-length tensors instead of EmbeddingBag for GPU efficiency.
All data pipelines use numpy arrays — no Python-level loops on millions of rows.
No DataLoader — training loop does direct tensor slicing for maximum throughput.
"""

import numpy as np
import torch

MAX_TYPES_PER_PROBLEM = 10


def preprocess_features(problems, users, submissions, problem_types, num_types=None):
    """Build feature tensors indexed by new_pid / new_uid for direct batch slicing."""

    # --- Problem types ---
    if num_types is None:
        num_types = (
            int(problem_types["type_id"].max()) + 1 if len(problem_types) > 0 else 1
        )

    pid_to_new = dict(zip(problems["pid"], problems["new_pid"]))
    problem_types = problem_types.copy()
    problem_types["new_pid"] = problem_types["pid"].map(pid_to_new)
    problem_types = problem_types.dropna(subset=["new_pid"])
    problem_types["new_pid"] = problem_types["new_pid"].astype(int)

    max_pid = int(problems["new_pid"].max()) + 1
    type_groups = problem_types.groupby("new_pid")["type_id"].apply(list).to_dict()

    padded_types = np.zeros((max_pid, MAX_TYPES_PER_PROBLEM), dtype=np.int64)
    type_counts = np.zeros(max_pid, dtype=np.int64)
    for pid, types in type_groups.items():
        if pid >= max_pid:
            continue
        n = min(len(types), MAX_TYPES_PER_PROBLEM)
        padded_types[pid, :n] = [int(t) for t in types[:n]]
        type_counts[pid] = n

    # --- Problem continuous features (vectorized) ---
    problems = problems.copy()
    problems["ac_rate"] = problems["ac_rate"].fillna(0)
    problems["user_count"] = problems["user_count"].fillna(0)
    problems["group_id"] = problems["group_id"].fillna(0).astype(int)
    problems["time_limit"] = problems["time_limit"].fillna(1.0)
    problems["memory_limit"] = problems["memory_limit"].fillna(256)
    problems["points"] = problems["points"].fillna(1.0)

    num_groups = int(problems["group_id"].max()) + 1

    prob_continuous = np.zeros((max_pid, 5), dtype=np.float32)
    pids_arr = problems["new_pid"].values.astype(int)
    prob_continuous[pids_arr, 0] = problems["ac_rate"].values.astype(np.float32) / 100.0
    prob_continuous[pids_arr, 1] = np.log(
        np.maximum(problems["points"].values.astype(np.float32), 1.0)
    )
    prob_continuous[pids_arr, 2] = np.log(
        np.maximum(problems["user_count"].values.astype(np.float32), 1.0)
    )
    prob_continuous[pids_arr, 3] = np.log(
        np.maximum(problems["time_limit"].values.astype(np.float32), 0.1)
    )
    prob_continuous[pids_arr, 4] = np.log(
        np.maximum(problems["memory_limit"].values.astype(np.float32), 1.0)
    )

    prob_group_ids = np.zeros(max_pid, dtype=np.int64)
    prob_group_ids[pids_arr] = problems["group_id"].values.astype(np.int64)

    # --- User features (vectorized) ---
    max_uid = int(users["new_uid"].max()) + 1
    users = users.copy()
    users["rating"] = users["rating"].fillna(0)
    users["points"] = users["points"].fillna(0)
    if "problem_count" in users.columns:
        users["problem_count"] = users["problem_count"].fillna(0)

    uids_arr = users["new_uid"].values.astype(int)
    rating_vals = users["rating"].values.astype(np.float32)

    user_continuous = np.zeros((max_uid, 4), dtype=np.float32)
    user_continuous[uids_arr, 0] = rating_vals / 3000.0
    user_continuous[uids_arr, 1] = (rating_vals > 0).astype(np.float32)
    user_continuous[uids_arr, 2] = np.log(
        np.maximum(users["points"].values.astype(np.float32), 1.0)
    )
    if "problem_count" in users.columns:
        user_continuous[uids_arr, 3] = np.log(
            np.maximum(users["problem_count"].values.astype(np.float32), 1.0)
        )

    # --- User solved-type profile (vectorized) ---
    solved = (
        submissions[submissions["solved"] == 1]
        if "solved" in submissions.columns
        else submissions
    )

    user_type_profile = np.zeros((max_uid, num_types), dtype=np.float32)
    user_solved_counts = np.zeros(max_uid, dtype=np.float32)

    solved_with_types = solved[["new_uid", "new_pid"]].merge(
        problem_types[["new_pid", "type_id"]], on="new_pid", how="inner"
    )
    if len(solved_with_types) > 0:
        uid_vals = solved_with_types["new_uid"].values.astype(int)
        type_vals = solved_with_types["type_id"].values.astype(int)
        valid = type_vals < num_types
        np.add.at(user_type_profile, (uid_vals[valid], type_vals[valid]), 1.0)

    solved_counts = solved.groupby("new_uid").size()
    sc_uids = solved_counts.index.values.astype(int)
    sc_vals = solved_counts.values.astype(np.float32)
    valid_uids = sc_uids < max_uid
    user_solved_counts[sc_uids[valid_uids]] = sc_vals[valid_uids]

    mask = user_solved_counts > 0
    user_type_profile[mask] /= user_solved_counts[mask, np.newaxis]

    problem_features = {
        "type_ids": torch.from_numpy(padded_types),
        "type_counts": torch.from_numpy(type_counts),
        "group_ids": torch.from_numpy(prob_group_ids),
        "continuous": torch.from_numpy(prob_continuous),
    }

    user_features = {
        "user_ids": torch.arange(max_uid, dtype=torch.long),
        "continuous": torch.from_numpy(user_continuous),
        "type_profile": torch.from_numpy(user_type_profile),
    }

    return problem_features, user_features, num_types, num_groups


def build_interactions(submissions, bookmarks=None, votes=None, id_maps=None):
    """
    Build interaction arrays from multiple signals (fully vectorized).
    Returns (uids, pids, labels) as numpy arrays.
    """
    all_uids = []
    all_pids = []
    all_labels = []

    sub_uids = submissions["new_uid"].values.astype(np.int64)
    sub_pids = submissions["new_pid"].values.astype(np.int64)
    if "solved" in submissions.columns:
        solved = submissions["solved"].values.astype(int)
        sub_labels = np.where(solved == 1, 1.0, 0.7).astype(np.float32)
    else:
        sub_labels = np.ones(len(submissions), dtype=np.float32)
    all_uids.append(sub_uids)
    all_pids.append(sub_pids)
    all_labels.append(sub_labels)

    uid_to_new = {v: k for k, v in id_maps["uid"].items()} if id_maps else {}
    pid_to_new = {v: k for k, v in id_maps["pid"].items()} if id_maps else {}

    if bookmarks is not None and len(bookmarks) > 0:
        bm = bookmarks.copy()
        bm["new_pid"] = bm["pid"].map(pid_to_new)
        bm["new_uid"] = bm["uid"].map(uid_to_new)
        bm = bm.dropna(subset=["new_pid", "new_uid"])
        if len(bm) > 0:
            all_uids.append(bm["new_uid"].values.astype(np.int64))
            all_pids.append(bm["new_pid"].values.astype(np.int64))
            all_labels.append(np.full(len(bm), 0.9, dtype=np.float32))

    if votes is not None and len(votes) > 0:
        vt = votes.copy()
        vt["new_pid"] = vt["pid"].map(pid_to_new)
        vt["new_uid"] = vt["uid"].map(uid_to_new)
        vt = vt.dropna(subset=["new_pid", "new_uid"])
        if len(vt) > 0:
            scores = vt["score"].values.astype(int)
            vlabels = np.where(scores > 0, 0.8, 0.0).astype(np.float32)
            all_uids.append(vt["new_uid"].values.astype(np.int64))
            all_pids.append(vt["new_pid"].values.astype(np.int64))
            all_labels.append(vlabels)

    return (
        np.concatenate(all_uids),
        np.concatenate(all_pids),
        np.concatenate(all_labels),
    )


def materialize_dataset(uids, pids, labels, num_problems, neg_ratio=4, seed=42):
    """
    Pre-materialize all samples (positives + negatives) as torch tensors.
    Returns (all_uids, all_pids, all_labels) tensors ready for direct slicing.
    """
    rng = np.random.RandomState(seed)

    pos_mask = labels > 0
    pos_uids = uids[pos_mask]
    pos_pids = pids[pos_mask]
    pos_labels = labels[pos_mask]
    n_pos = len(pos_uids)

    neg_uids = uids[~pos_mask]
    neg_pids = pids[~pos_mask]
    n_neg = len(neg_uids)

    # Popularity-weighted negative sampling (vectorized)
    pid_counts = np.zeros(num_problems, dtype=np.float32)
    np.add.at(pid_counts, pos_pids, 1.0)
    pop_weights = np.power(pid_counts + 1.0, 0.75)
    pop_weights[0] = 0
    pop_weights /= pop_weights.sum()

    n_random = n_pos * neg_ratio
    rand_uids = np.repeat(pos_uids, neg_ratio)
    rand_pids = rng.choice(num_problems, size=n_random, p=pop_weights).astype(np.int64)

    out_uids = np.concatenate([pos_uids, neg_uids, rand_uids])
    out_pids = np.concatenate([pos_pids, neg_pids, rand_pids])
    out_labels = np.concatenate(
        [
            pos_labels,
            np.zeros(n_neg, dtype=np.float32),
            np.zeros(n_random, dtype=np.float32),
        ]
    )

    print(
        f"  Dataset: {n_pos} pos + {n_neg} explicit neg + {n_random} random neg = {len(out_uids)} total"
    )

    return (
        torch.from_numpy(out_uids),
        torch.from_numpy(out_pids),
        torch.from_numpy(out_labels),
    )


def get_batch(uids, pids, labels, idx, problem_features, user_features, device):
    """
    Build a batch from pre-materialized tensors using pure tensor indexing.
    idx: 1D LongTensor of sample indices.
    Returns (user_feat_dict, prob_feat_dict, labels) all on device.
    """
    b_uids = uids[idx]
    b_pids = pids[idx]
    b_labels = labels[idx].to(device)

    user_feat = {
        "user_ids": b_uids.to(device),
        "continuous": user_features["continuous"][b_uids].to(device),
        "type_profile": user_features["type_profile"][b_uids].to(device),
    }
    prob_feat = {
        "type_ids": problem_features["type_ids"][b_pids].to(device),
        "type_counts": problem_features["type_counts"][b_pids].to(device),
        "group_ids": problem_features["group_ids"][b_pids].to(device),
        "continuous": problem_features["continuous"][b_pids].to(device),
    }
    return user_feat, prob_feat, b_labels
