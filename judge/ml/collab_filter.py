import numpy as np
import os
import hashlib

from django.core.cache import cache
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
        self.problem_embeddings = self.embeddings[problem_arr]

    def __str__(self):
        return self.name

    def compute_scores(self, query_embedding, item_embeddings, measure=DOT):
        """Computes the scores of the candidates given a query.
        Args:
        query_embedding: a vector of shape [k], representing the query embedding.
        item_embeddings: a matrix of shape [N, k], such that row i is the embedding
            of item i.
        measure: a string specifying the similarity measure to be used. Can be
            either DOT or COSINE.
        Returns:
        scores: a vector of shape [N], such that scores[i] is the score of item i.
        """
        u = query_embedding
        V = item_embeddings
        if measure == self.COSINE:
            V = V / np.linalg.norm(V, axis=1, keepdims=True)
            u = u / np.linalg.norm(u)
        scores = u.dot(V.T)
        return scores

    def _get_embedding_version(self):
        first_problem = self.problem_embeddings[0]
        array_bytes = first_problem.tobytes()
        hash_object = hashlib.sha256(array_bytes)
        hash_bytes = hash_object.digest()
        return hash_bytes.hex()[:5]

    @cache_wrapper(prefix="CFgue", timeout=86400)
    def _get_user_embedding(self, user_id, embedding_version):
        user_arr, _ = self.embeddings.files
        user_embeddings = self.embeddings[user_arr]
        if user_id >= len(user_embeddings):
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
            if pid < len(scores):
                res.append((scores[pid], pid))

        res.sort(reverse=True, key=lambda x: x[0])
        res = res[:limit]
        return res

    # return a list of pid
    def problem_neighbors(self, problem, problemset, measure=DOT, limit=None):
        pid = problem.id
        if pid >= len(self.problem_embeddings):
            return []
        scores = self.compute_scores(
            self.problem_embeddings[pid], self.problem_embeddings, measure
        )
        res = []
        for p in problemset:
            if p < len(scores):
                res.append((scores[p], p))
        res.sort(reverse=True, key=lambda x: x[0])
        return res[:limit]
