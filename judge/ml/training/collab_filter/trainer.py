"""
Trainer for collaborative filtering models (collab_filter, collab_filter_time).
"""

try:
    from judge.ml.training.collab_filter.model import CFModel
    from judge.ml.training.data_utils import (
        apply_time_decay,
        build_sparse_tensor,
        load_data,
        reindex_data,
        save_embeddings,
        split_data,
    )
except ImportError:
    # Running on Modal without Django
    from collab_filter.model import CFModel
    from data_utils import (
        apply_time_decay,
        build_sparse_tensor,
        load_data,
        reindex_data,
        save_embeddings,
        split_data,
    )


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
    Train a collaborative filtering model.

    Returns (uid_embeddings, pid_embeddings) dicts mapping original IDs to numpy arrays.
    If output_npz is set, also saves to that path.
    """
    problems, users, submissions = load_data(data_path)
    problems, users, submissions, id_maps = reindex_data(problems, users, submissions)

    if model_name == "collab_filter_time":
        submissions = apply_time_decay(submissions)

    train_df, test_df = split_data(submissions)

    num_users = int(users["new_uid"].max()) + 1
    num_problems = int(problems["new_pid"].max()) + 1
    value_col = "value" if "value" in train_df.columns else None

    train_sparse = build_sparse_tensor(train_df, num_users, num_problems, value_col)
    test_sparse = build_sparse_tensor(test_df, num_users, num_problems, value_col)

    model = CFModel(num_users, num_problems, embedding_dim)
    U, V = model.train(
        train_sparse,
        test_sparse,
        num_iterations=iterations,
        lr=lr,
        log_path=log_path,
        model_name=model_name,
    )

    uid_embeddings = {id_maps["uid"][i]: emb for i, emb in enumerate(U)}
    pid_embeddings = {id_maps["pid"][i]: emb for i, emb in enumerate(V)}

    if output_npz:
        save_embeddings(output_npz, uid_embeddings, pid_embeddings)

    return uid_embeddings, pid_embeddings
