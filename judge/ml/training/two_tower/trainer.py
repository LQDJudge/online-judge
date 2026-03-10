"""
Trainer for Two Tower recommendation model.
"""

import os

try:
    from judge.ml.training.two_tower.model import TwoTowerModel
    from judge.ml.training.two_tower.dataset import (
        build_interactions,
        materialize_dataset,
        preprocess_features,
    )
    from judge.ml.training.data_utils import (
        load_data,
        reindex_data,
        save_embeddings,
    )
except ImportError:
    from two_tower.model import TwoTowerModel
    from two_tower.dataset import (
        build_interactions,
        materialize_dataset,
        preprocess_features,
    )
    from data_utils import (
        load_data,
        reindex_data,
        save_embeddings,
    )

import numpy as np
import pandas as pd


def train_model(
    model_name,
    data_path,
    embedding_dim=50,
    iterations=2000,
    lr=150.0,
    log_path=None,
    output_npz=None,
):
    """
    Train a Two Tower model.

    Returns (uid_embeddings, pid_embeddings) dicts mapping original IDs to numpy arrays.
    If output_npz is set, also saves to that path.
    """
    problems, users, submissions = load_data(data_path)

    problem_types_path = os.path.join(data_path, "problem_types.csv")
    problem_types = (
        pd.read_csv(problem_types_path)
        if os.path.exists(problem_types_path)
        else pd.DataFrame(columns=["pid", "type_id"])
    )

    bookmarks_path = os.path.join(data_path, "problem_bookmarks.csv")
    bookmarks = pd.read_csv(bookmarks_path) if os.path.exists(bookmarks_path) else None

    votes_path = os.path.join(data_path, "problem_votes.csv")
    votes = pd.read_csv(votes_path) if os.path.exists(votes_path) else None

    problems, users, submissions, id_maps = reindex_data(problems, users, submissions)

    num_users = int(users["new_uid"].max()) + 1
    num_problems = int(problems["new_pid"].max()) + 1

    print("Preprocessing features...")
    problem_features, user_features, num_types, num_groups = preprocess_features(
        problems, users, submissions, problem_types
    )

    print("Building interactions...")
    i_uids, i_pids, i_labels = build_interactions(
        submissions, bookmarks, votes, id_maps
    )
    print(f"  Total interactions: {len(i_uids)}")

    # Train/val split (90/10)
    rng = np.random.RandomState(42)
    indices = rng.permutation(len(i_uids))
    split = int(0.9 * len(indices))
    train_idx, val_idx = indices[:split], indices[split:]

    print("Materializing datasets...")
    train_data = materialize_dataset(
        i_uids[train_idx],
        i_pids[train_idx],
        i_labels[train_idx],
        num_problems,
        neg_ratio=4,
        seed=42,
    )
    val_data = materialize_dataset(
        i_uids[val_idx],
        i_pids[val_idx],
        i_labels[val_idx],
        num_problems,
        neg_ratio=4,
        seed=123,
    )

    num_epochs = min(iterations, 15)
    tt_lr = 3e-3
    temperature = 0.2

    print(
        f"Model: {num_users} users, {num_problems} problems, {num_types} types, {num_groups} groups"
    )
    print(
        f"Hyperparams: epochs={num_epochs}, lr={tt_lr}, temp={temperature}, dim={embedding_dim}"
    )

    model = TwoTowerModel(
        num_users=num_users,
        num_types=num_types,
        num_groups=num_groups,
        embedding_dim=embedding_dim,
        temperature=temperature,
    )

    model.train_loop(
        train_data,
        val_data,
        problem_features,
        user_features,
        batch_size=2048,
        num_epochs=num_epochs,
        lr=tt_lr,
        log_path=log_path,
        model_name=model_name,
    )

    # Extract all embeddings
    print("Extracting embeddings...")
    all_user_emb = model.get_all_user_embeddings(user_features)
    all_prob_emb = model.get_all_problem_embeddings(problem_features)

    uid_embeddings = {id_maps["uid"][i]: all_user_emb[i] for i in range(num_users)}
    pid_embeddings = {id_maps["pid"][i]: all_prob_emb[i] for i in range(num_problems)}

    if output_npz:
        save_embeddings(output_npz, uid_embeddings, pid_embeddings)

    return uid_embeddings, pid_embeddings
