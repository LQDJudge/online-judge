# ML Recommendations

Problem recommendation system using collaborative filtering with MariaDB vector search.

## Requirements

- MariaDB 11.7+ (for VECTOR column type and `VEC_DISTANCE_COSINE()`)
- PyTorch with CUDA (optional, for GPU-accelerated training)

Install ML dependencies:
```bash
pip install -r judge/ml/requirements.txt
```

For GPU training locally, install PyTorch with CUDA support:
```bash
pip install torch --index-url https://download.pytorch.org/whl/cu124
```

## Setup

### 1. Create vector tables

```bash
./judge/ml/setup.sh
```

This runs all numbered `.sql` files in `judge/sql/` against your MariaDB database.
Connection settings are read from Django's `DATABASES['default']`.
You can also run a specific file: `./judge/ml/setup.sh 001`

### 2. Generate training data

Export problems, users, and submissions to CSV:
```bash
python manage.py generate_data --output /tmp/ml_data/
```

### 3. Train models

Two options: train locally or on Modal (GPU cloud). Both produce `.npz` files that are then imported to the database.

#### Option A: Train locally

```bash
# Train all models at once
python manage.py train_embeddings --all --data-path /tmp/ml_data/ --output-dir /tmp/ml_embeddings/
python manage.py import_embeddings --all --dir /tmp/ml_embeddings/

# Or train a single model
python manage.py train_embeddings --model collab_filter --data-path /tmp/ml_data/ --output /tmp/collab_filter.npz
python manage.py import_embeddings --model collab_filter --file /tmp/collab_filter.npz
```

Training options:
- `--iterations N` — training iterations (default: 2000)
- `--embedding-dim N` — embedding dimensions (default: 50)
- `--lr N` — learning rate (default: 150.0)
- `--log-path /path/` — save training loss plots

#### Option B: Train on Modal (GPU cloud)

[Modal](https://modal.com) provides serverless GPU instances. Install with `pip install modal` and set up auth with `modal setup`.

```bash
# Train all models on GPU
modal run judge/ml/training/modal_runner.py --data-path /tmp/ml_data/

# Download from Modal and import to DB (one command)
python manage.py import_embeddings --all --from-modal

# Or download and import a single model manually
modal volume get lqdoj-ml-volume /collab_filter/embeddings.npz /tmp/collab_filter.npz
python manage.py import_embeddings --model collab_filter --file /tmp/collab_filter.npz
```

### 4. Enable recommendations

In `local_settings.py`:
```python
USE_ML = True
```

## Importing pre-trained embeddings

If you have `.npz` files from a previous training run:
```bash
# Import a single model
python manage.py import_embeddings --model collab_filter --file /path/to/embeddings.npz

# Import all models from a directory (expects collab_filter/embeddings.npz, collab_filter_time/embeddings.npz)
python manage.py import_embeddings --all --dir /path/to/ml_output/
```

## Evaluation

Evaluate recommendation quality with standard IR metrics:
```bash
python judge/ml/evaluate.py
python judge/ml/evaluate.py --K 5 10 20 --sample-users 2000 --models collab_filter collab_filter_time popular random
```

Options:
- `--K` — top-K values to evaluate (default: 5 10 20 50)
- `--holdout N` — problems per user to hold out as ground truth (default: 5)
- `--sample-users N` — number of users to sample (default: 1000)
- `--min-submissions N` — minimum submissions to include user (default: 20)
- `--models` — models to compare (default: collab_filter collab_filter_time popular random)

Metrics reported: Hit Rate, Precision@K, Recall@K, NDCG@K, MRR.

## Quick start

```bash
./judge/ml/setup.sh
python manage.py generate_data --output /tmp/ml_data/
python manage.py train_embeddings --all --data-path /tmp/ml_data/ --output-dir /tmp/ml_embeddings/
python manage.py import_embeddings --all --dir /tmp/ml_embeddings/
# Then set USE_ML = True in local_settings.py
```

## How it works

- **Training:** Matrix factorization learns 50-dimensional embeddings for users and problems from submission history. The loss function combines MSE on observed submissions, L2 regularization, and a gravity term.
- **Serving:** Embeddings are stored in MariaDB VECTOR columns with HNSW indexes. Recommendations use `VEC_DISTANCE_COSINE()` for O(log n) approximate nearest-neighbor search.
- **Two models:**
  - `collab_filter` — treats all submissions equally
  - `collab_filter_time` — weights recent submissions higher (decay=0.994)

## Adding a new model

1. Create `judge/ml/training/<model_name>/` with `model.py` and `trainer.py`
2. `trainer.py` must export `train_model(model_name, data_path, embedding_dim, iterations, lr, log_path, output_npz)` returning `(uid_embeddings, pid_embeddings)`
3. Register in `judge/ml/training/__init__.py` MODELS dict
4. Add tables in a new SQL file (e.g. `judge/sql/002_<model>_tables.sql`)
5. Add table mapping in `judge/ml/vector_store.py` TABLE_MAP
6. Modal runner reads from `MODELS` automatically — no extra step needed

## File structure

```
judge/ml/
├── README.md              # This file
├── requirements.txt       # ML dependencies (torch, numpy, pandas, matplotlib, modal)
├── setup.sh               # Run SQL migrations
├── vector_store.py        # MariaDB vector search (public API)
├── evaluate.py            # Evaluation script
└── training/
    ├── __init__.py             # Model registry (MODELS dict, get_trainer)
    ├── data_utils.py           # Shared data loading/preprocessing
    ├── modal_runner.py         # Modal cloud training
    └── collab_filter/          # Collaborative filtering models
        ├── model.py            # PyTorch CF model
        └── trainer.py          # CF training pipeline

judge/sql/
└── 001_ml_vector_tables.sql   # Vector table DDL (idempotent)

judge/management/commands/
├── generate_data.py           # Export training data to CSV
├── train_embeddings.py        # Train and save .npz (uses registry)
└── import_embeddings.py       # Import .npz to MariaDB
```
