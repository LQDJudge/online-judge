"""
Shared data loading and preprocessing for ML training.
No Django dependency — can run standalone or on Modal.
"""

import os

import numpy as np
import pandas as pd
import torch


def load_data(data_path):
    """Load CSVs and return (problems, users, submissions) DataFrames."""
    problems = pd.read_csv(os.path.join(data_path, "problems.csv"))
    users = pd.read_csv(os.path.join(data_path, "profiles.csv"))
    submissions = pd.read_csv(os.path.join(data_path, "submissions.csv")).dropna()
    return problems, users, submissions


def reindex_data(problems, users, submissions):
    """
    Assign sequential indices (1-based) to problems and users.
    Returns (problems, users, submissions) with new_pid/new_uid columns,
    plus id_maps = {'uid': {new_uid: orig_uid}, 'pid': {new_pid: orig_pid}}.
    """
    problems["new_pid"], _ = pd.factorize(problems["pid"], sort=True)
    problems["new_pid"] = (problems["new_pid"] + 1).astype(int)

    users["new_uid"], _ = pd.factorize(users["uid"], sort=True)
    users["new_uid"] = (users["new_uid"] + 1).astype(int)

    submissions = submissions.merge(users[["uid", "new_uid"]], on="uid", how="left")
    submissions = submissions.merge(problems[["pid", "new_pid"]], on="pid", how="left")
    submissions = submissions.dropna(subset=["new_uid", "new_pid"])
    submissions["new_uid"] = submissions["new_uid"].astype(int)
    submissions["new_pid"] = submissions["new_pid"].astype(int)

    uid_map = pd.Series(users["uid"].values, index=users["new_uid"]).to_dict()
    uid_map[0] = 0
    pid_map = pd.Series(problems["pid"].values, index=problems["new_pid"]).to_dict()
    pid_map[0] = 0

    return problems, users, submissions, {"uid": uid_map, "pid": pid_map}


def split_data(submissions, holdout_fraction=0.1):
    """Split submissions into train/test DataFrames."""
    test = submissions.sample(frac=holdout_fraction, replace=False)
    train = submissions[~submissions.index.isin(test.index)]
    return train, test


def build_sparse_tensor(submission_df, num_users, num_problems, value_col=None):
    """
    Build a sparse COO tensor from submission DataFrame.
    If value_col is provided, use that column as values (for time-weighted).
    Otherwise all values are 1.
    """
    indices = submission_df[["new_uid", "new_pid"]].values.T  # (2, N)
    if value_col and value_col in submission_df.columns:
        values = submission_df[value_col].values.astype(np.float32)
    else:
        values = np.ones(len(submission_df), dtype=np.float32)

    return torch.sparse_coo_tensor(
        torch.tensor(indices, dtype=torch.long),
        torch.tensor(values),
        size=(num_users, num_problems),
    ).coalesce()


def save_embeddings(output_npz, uid_embeddings, pid_embeddings):
    """Save embedding dicts to .npz file."""
    output_dir = os.path.dirname(output_npz)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    np.savez(output_npz, uid_embeddings, pid_embeddings)
    print(f"Saved to {output_npz}")


def apply_time_decay(submissions, decay=0.994, min_weight=0.6):
    """
    Apply time-decay weighting to submissions.
    More recent submissions per user get higher weight.
    Submissions must be ordered chronologically (oldest first).
    """
    max_uid = int(submissions["uid"].max()) + 1
    current_weight = np.ones(max_uid, dtype=np.float32)
    weights = np.empty(len(submissions), dtype=np.float32)

    for i, uid in enumerate(submissions["uid"].values):
        uid = int(uid)
        weights[i] = current_weight[uid]
        current_weight[uid] = max(current_weight[uid] * decay, min_weight)

    submissions = submissions.copy()
    submissions["value"] = weights
    return submissions
