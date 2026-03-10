"""
Two Tower recommendation model with side features.

Two neural network towers produce L2-normalized embeddings:
- Problem Tower: problem types + group + continuous features -> 50-dim
- User Tower: user ID + continuous features + solved-type profile -> 50-dim

Training uses BCE loss with temperature-scaled cosine similarity.
"""

import collections
import os
import time

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torch.nn.functional as F


def _get_device():
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


class ProblemTower(nn.Module):
    def __init__(self, num_types, num_groups, embedding_dim=50):
        super().__init__()
        self.type_emb = nn.Embedding(num_types + 1, 32, padding_idx=0)
        self.group_emb = nn.Embedding(num_groups + 1, 16, padding_idx=0)
        self.mlp = nn.Sequential(
            nn.Linear(53, 128),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(64, embedding_dim),
        )

    def forward(self, type_ids, type_counts, group_ids, continuous):
        t = self.type_emb(type_ids)  # (B, max_types, 32)
        mask = (type_ids != 0).unsqueeze(-1).float()
        t = (t * mask).sum(dim=1) / type_counts.clamp(min=1).unsqueeze(-1).float()
        g = self.group_emb(group_ids)
        x = torch.cat([t, g, continuous], dim=1)
        return F.normalize(self.mlp(x), p=2, dim=1)


class UserTower(nn.Module):
    def __init__(self, num_users, num_types, embedding_dim=50):
        super().__init__()
        self.user_emb = nn.Embedding(num_users + 1, 32, padding_idx=0)
        self.type_proj = nn.Linear(num_types, 32)
        self.mlp = nn.Sequential(
            nn.Linear(68, 128),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Dropout(0.2),
            nn.Linear(64, embedding_dim),
        )

    def forward(self, user_ids, continuous, type_profile):
        u = self.user_emb(user_ids)
        tp = self.type_proj(type_profile)
        x = torch.cat([u, continuous, tp], dim=1)
        return F.normalize(self.mlp(x), p=2, dim=1)


class TwoTowerModel(nn.Module):
    def __init__(
        self, num_users, num_types, num_groups, embedding_dim=50, temperature=0.07
    ):
        super().__init__()
        self.problem_tower = ProblemTower(num_types, num_groups, embedding_dim)
        self.user_tower = UserTower(num_users, num_types, embedding_dim)
        self.temperature = temperature
        self.device = _get_device()
        self.to(self.device)

    def forward(self, user_features, problem_features):
        user_emb = self.user_tower(
            user_features["user_ids"],
            user_features["continuous"],
            user_features["type_profile"],
        )
        prob_emb = self.problem_tower(
            problem_features["type_ids"],
            problem_features["type_counts"],
            problem_features["group_ids"],
            problem_features["continuous"],
        )
        return (user_emb * prob_emb).sum(dim=1) / self.temperature

    def train_loop(
        self,
        train_data,
        val_data,
        problem_features,
        user_features,
        batch_size=2048,
        num_epochs=30,
        lr=1e-3,
        log_path=None,
        model_name="two_tower",
    ):
        """
        Train with BCE loss using direct tensor batching (no DataLoader).

        train_data / val_data: (uids, pids, labels) tensors from materialize_dataset.
        problem_features / user_features: dicts of feature tensors indexed by ID.
        """
        try:
            from judge.ml.training.two_tower.dataset import get_batch
        except ImportError:
            from two_tower.dataset import get_batch

        optimizer = torch.optim.Adam(self.parameters(), lr=lr)
        criterion = nn.BCEWithLogitsLoss()

        train_uids, train_pids, train_labels = train_data
        val_uids, val_pids, val_labels = val_data
        n_train = len(train_uids)
        n_val = len(val_uids)

        history = collections.defaultdict(list)
        best_val_loss = float("inf")
        best_state = None

        print(
            f"Training on {self.device} — {num_epochs} epochs, {n_train} train, {n_val} val"
        )
        for epoch in range(num_epochs):
            t0 = time.time()

            # --- Train ---
            self.train()
            perm = torch.randperm(n_train)
            train_loss = 0.0
            n_batches = 0

            for start in range(0, n_train, batch_size):
                idx = perm[start : start + batch_size]
                user_feat, prob_feat, labels = get_batch(
                    train_uids,
                    train_pids,
                    train_labels,
                    idx,
                    problem_features,
                    user_features,
                    self.device,
                )

                optimizer.zero_grad()
                scores = self.forward(user_feat, prob_feat)
                loss = criterion(scores, labels)
                loss.backward()
                optimizer.step()

                train_loss += loss.item()
                n_batches += 1

            avg_train = train_loss / max(n_batches, 1)

            # --- Validation ---
            self.eval()
            val_loss = 0.0
            v_batches = 0
            with torch.no_grad():
                for start in range(0, n_val, batch_size * 2):
                    idx = torch.arange(start, min(start + batch_size * 2, n_val))
                    user_feat, prob_feat, labels = get_batch(
                        val_uids,
                        val_pids,
                        val_labels,
                        idx,
                        problem_features,
                        user_features,
                        self.device,
                    )
                    scores = self.forward(user_feat, prob_feat)
                    loss = criterion(scores, labels)
                    val_loss += loss.item()
                    v_batches += 1

            avg_val = val_loss / max(v_batches, 1)
            history["train_loss"].append(avg_train)
            history["val_loss"].append(avg_val)

            elapsed = time.time() - t0
            print(
                f"Epoch {epoch+1}/{num_epochs}: train={avg_train:.4f}, val={avg_val:.4f} ({elapsed:.1f}s)"
            )

            if avg_val < best_val_loss:
                best_val_loss = avg_val
                best_state = {k: v.cpu().clone() for k, v in self.state_dict().items()}

        if best_state:
            self.load_state_dict(best_state)
            self.to(self.device)

        if log_path:
            self._plot_metrics(history, log_path, model_name)

        return history

    def _plot_metrics(self, history, log_path, model_name):
        os.makedirs(log_path, exist_ok=True)
        fig, ax = plt.subplots(figsize=(10, 6))
        epochs = range(1, len(history["train_loss"]) + 1)
        ax.plot(epochs, history["train_loss"], label="train_loss")
        ax.plot(epochs, history["val_loss"], label="val_loss")
        ax.set_xlabel("Epoch")
        ax.set_ylabel("BCE Loss")
        ax.legend()
        ax.set_title(f"{model_name} Training")
        plt.tight_layout()
        plt.savefig(os.path.join(log_path, f"{model_name}.png"))
        plt.close()
        print(f"Plot saved to {os.path.join(log_path, f'{model_name}.png')}")

    @torch.no_grad()
    def get_all_user_embeddings(self, user_features):
        """Extract embeddings for all users in batches. Returns (N, dim) numpy."""
        self.eval()
        n = user_features["user_ids"].shape[0]
        embs = []
        for start in range(0, n, 4096):
            end = min(start + 4096, n)
            emb = self.user_tower(
                user_features["user_ids"][start:end].to(self.device),
                user_features["continuous"][start:end].to(self.device),
                user_features["type_profile"][start:end].to(self.device),
            )
            embs.append(emb.cpu())
        return torch.cat(embs, dim=0).numpy()

    @torch.no_grad()
    def get_all_problem_embeddings(self, problem_features):
        """Extract embeddings for all problems in batches. Returns (N, dim) numpy."""
        self.eval()
        n = problem_features["type_ids"].shape[0]
        embs = []
        for start in range(0, n, 4096):
            end = min(start + 4096, n)
            emb = self.problem_tower(
                problem_features["type_ids"][start:end].to(self.device),
                problem_features["type_counts"][start:end].to(self.device),
                problem_features["group_ids"][start:end].to(self.device),
                problem_features["continuous"][start:end].to(self.device),
            )
            embs.append(emb.cpu())
        return torch.cat(embs, dim=0).numpy()
