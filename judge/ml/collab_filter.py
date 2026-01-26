import numpy as np
import os
import hashlib

from django.conf import settings

from judge.caching import cache_wrapper


class CollabFilter:
    DOT = "dot"
    COSINE = "cosine"

    # name = 'collab_filter' or 'collab_filter_time'
    def __init__(self, name):
        self.embeddings = np.load(
            os.path.join(settings.ML_OUTPUT_PATH, name + "/embeddings.npz"),
            allow_pickle=True,
        )
        _, problem_arr = self.embeddings.files
        self.name = name
        self.problem_embeddings = self.embeddings[problem_arr].item()

    def __str__(self):
        return self.name

    def compute_scores(self, query_embedding, item_embeddings, measure=DOT):
        """Return {id: score}"""
        u = query_embedding
        V = np.stack(list(item_embeddings.values()))
        if measure == self.COSINE:
            V = V / np.linalg.norm(V, axis=1, keepdims=True)
            u = u / np.linalg.norm(u)
        scores = u.dot(V.T)
        scores_by_id = {id_: s for id_, s in zip(item_embeddings.keys(), scores)}
        return scores_by_id

    def _get_embedding_version(self):
        first_problem = self.problem_embeddings[0]
        array_bytes = first_problem.tobytes()
        hash_object = hashlib.sha256(array_bytes)
        hash_bytes = hash_object.digest()
        return hash_bytes.hex()[:5]

    @cache_wrapper(prefix="CFgue", timeout=86400)
    def _get_user_embedding(self, user_id, embedding_version):
        user_arr, _ = self.embeddings.files
        user_embeddings = self.embeddings[user_arr].item()
        if user_id not in user_embeddings:
            return user_embeddings[0]
        return user_embeddings[user_id]

    def get_user_embedding(self, user_id):
        version = self._get_embedding_version()
        return self._get_user_embedding(user_id, version)

    @cache_wrapper(prefix="user_recommendations", timeout=3600)
    def user_recommendations(self, user_id, problems, measure=DOT, limit=None):
        user_embedding = self.get_user_embedding(user_id)
        scores = self.compute_scores(user_embedding, self.problem_embeddings, measure)
        res = []  # [(score, problem)]
        for pid in problems:
            if pid in scores:
                res.append((scores[pid], pid))

        res.sort(reverse=True, key=lambda x: x[0])
        return res[:limit]

    # return a list of pid
    def problem_neighbors(self, problem, problemset, measure=DOT, limit=None):
        pid = problem.id
        if pid not in self.problem_embeddings:
            return []
        embedding = self.problem_embeddings[pid]
        scores = self.compute_scores(embedding, self.problem_embeddings, measure)
        res = []
        for p in problemset:
            if p in scores:
                res.append((scores[p], p))
        res.sort(reverse=True, key=lambda x: x[0])
        return res[:limit]
