"""
Collaborative Filtering via Matrix Factorization in PyTorch.
Port of LQDOJ_ML/src/_collab_filter.py from TensorFlow.

Loss = MSE(observed) + regularization + gravity
"""

import collections
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import torch


def _get_device():
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


class CFModel:
    """Matrix factorization with regularization + gravity loss."""

    def __init__(self, num_users, num_problems, embedding_dim=50, init_stddev=0.5):
        self.device = _get_device()
        self.U = torch.nn.Parameter(
            torch.randn(num_users, embedding_dim, device=self.device) * init_stddev
        )
        self.V = torch.nn.Parameter(
            torch.randn(num_problems, embedding_dim, device=self.device) * init_stddev
        )

    def sparse_mse(self, sparse_tensor, U, V):
        """MSE on observed entries of the sparse submission matrix."""
        indices = sparse_tensor.indices()  # (2, nnz)
        values = sparse_tensor.values()  # (nnz,)
        user_emb = U[indices[0]]  # (nnz, dim)
        prob_emb = V[indices[1]]  # (nnz, dim)
        predictions = (user_emb * prob_emb).sum(dim=1)  # dot product per pair
        return torch.mean((predictions - values) ** 2)

    def gravity(self, U, V):
        """Gravity regularization to prevent embedding collapse."""
        num_u, num_v = U.shape[0], V.shape[0]
        return (1.0 / (num_u * num_v)) * torch.sum(torch.mm(U.T, U) * torch.mm(V.T, V))

    def train(
        self,
        train_sparse,
        test_sparse,
        num_iterations=2000,
        lr=150.0,
        reg_coeff=0.1,
        gravity_coeff=1.0,
        log_path=None,
        model_name="collab_filter",
    ):
        """Train the model and return (U, V) as numpy arrays."""
        # Move sparse tensors to device
        train_sparse = train_sparse.to(self.device)
        test_sparse = test_sparse.to(self.device)

        optimizer = torch.optim.SGD([self.U, self.V], lr=lr)

        history = collections.defaultdict(list)
        iterations = []

        print(f"Training on {self.device} — 0/{num_iterations}")
        for i in range(num_iterations + 1):
            optimizer.zero_grad()

            train_error = self.sparse_mse(train_sparse, self.U, self.V)
            reg_loss = reg_coeff * (
                torch.sum(self.U**2) / self.U.shape[0]
                + torch.sum(self.V**2) / self.V.shape[0]
            )
            grav_loss = gravity_coeff * self.gravity(self.U, self.V)
            total_loss = train_error + reg_loss + grav_loss

            total_loss.backward()
            optimizer.step()

            if i % 10 == 0 or i == num_iterations:
                with torch.no_grad():
                    test_error = self.sparse_mse(test_sparse, self.U, self.V)

                metrics = {
                    "train_error": train_error.item(),
                    "test_error": test_error.item(),
                    "reg_loss": reg_loss.item(),
                    "gravity_loss": grav_loss.item(),
                }
                iterations.append(i)
                for k, v in metrics.items():
                    history[k].append(v)

                print(
                    f"Iteration {i}: "
                    + ", ".join(f"{k}={v:.6f}" for k, v in metrics.items())
                )

        if log_path:
            self._plot_metrics(history, iterations, log_path, model_name)

        return self.U.detach().cpu().numpy(), self.V.detach().cpu().numpy()

    def _plot_metrics(self, history, iterations, log_path, model_name):
        """Save training metrics plot."""
        os.makedirs(log_path, exist_ok=True)
        fig, axes = plt.subplots(1, 2, figsize=(20, 8))

        axes[0].plot(iterations, history["train_error"], label="train_error")
        axes[0].plot(iterations, history["test_error"], label="test_error")
        axes[0].set_xlabel("Iteration")
        axes[0].legend()
        axes[0].set_title("Prediction Error")

        axes[1].plot(iterations, history["reg_loss"], label="reg_loss")
        axes[1].plot(iterations, history["gravity_loss"], label="gravity_loss")
        axes[1].set_xlabel("Iteration")
        axes[1].legend()
        axes[1].set_title("Regularization")

        plt.tight_layout()
        plt.savefig(os.path.join(log_path, f"{model_name}.png"))
        plt.close()
        print(f"Plot saved to {os.path.join(log_path, f'{model_name}.png')}")
