"""
Training registry. Maps model names to their trainer modules.

Each trainer module must have a train_model() function with signature:
    train_model(model_name, data_path, embedding_dim, iterations, lr,
                log_path, output_npz) -> (uid_embeddings, pid_embeddings)
"""

import importlib

# Model name -> relative trainer module path (under judge/ml/training/).
# When adding a new model, add it here.
MODELS = {
    "collab_filter": "collab_filter.trainer",
    "collab_filter_time": "collab_filter.trainer",
    "two_tower": "two_tower.trainer",
}


def get_trainer(model_name, prefix="judge.ml.training."):
    """Import and return the train_model function for a given model name.

    Args:
        prefix: Module prefix. Use "judge.ml.training." for Django context,
                "" for standalone (e.g. Modal).
    """
    if model_name not in MODELS:
        raise ValueError(
            f"Unknown model: {model_name}. Available: {list(MODELS.keys())}"
        )
    module = importlib.import_module(prefix + MODELS[model_name])
    return module.train_model
