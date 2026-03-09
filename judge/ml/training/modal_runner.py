"""
Modal cloud training runner.

Usage:
    # First generate data locally:
    python manage.py generate_data --output /tmp/ml_data/

    # Then train on Modal (uploads data automatically):
    modal run judge/ml/training/modal_runner.py --data-path /tmp/ml_data/

    # Download trained embeddings:
    modal volume get lqdoj-ml-volume /collab_filter/embeddings.npz /tmp/collab_filter.npz
    modal volume get lqdoj-ml-volume /collab_filter_time/embeddings.npz /tmp/collab_filter_time.npz

    # Import to database:
    python manage.py import_embeddings --model collab_filter --file /tmp/collab_filter.npz
    python manage.py import_embeddings --model collab_filter_time --file /tmp/collab_filter_time.npz
"""

import modal

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("torch", "numpy", "pandas", "matplotlib")
    .add_local_dir("judge/ml/training", remote_path="/root/training")
)

app = modal.App("LQDOJ_ML", image=image)
vol = modal.Volume.from_name("lqdoj-ml-volume", create_if_missing=True)


@app.function(
    volumes={"/data": vol},
    gpu="any",
    timeout=3600,
)
def train(model_name, trainer_module, iterations=2000, embedding_dim=50):
    import importlib
    import sys

    sys.path.insert(0, "/root/training")

    module = importlib.import_module(trainer_module)
    module.train_model(
        model_name,
        data_path="/data/ml_data",
        embedding_dim=embedding_dim,
        iterations=iterations,
        log_path="/data",
        output_npz=f"/data/{model_name}/embeddings.npz",
    )
    vol.commit()


@app.local_entrypoint()
def main(data_path: str = "/tmp/ml_data", iterations: int = 2000):
    from judge.ml.training import MODELS

    print(f"Uploading data from {data_path} to Modal volume...")
    with vol.batch_upload(force=True) as batch:
        batch.put_directory(data_path, "/ml_data")
    print("Data uploaded.")

    for model_name, trainer_module in MODELS.items():
        train.remote(model_name, trainer_module, iterations)

    print("Training complete. Download embeddings with:")
    for model_name in MODELS:
        print(
            f"  modal volume get lqdoj-ml-volume /{model_name}/embeddings.npz /tmp/{model_name}.npz"
        )
