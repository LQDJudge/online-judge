import numpy as np
from django.conf import settings
import os
from django.core.cache import cache
import hashlib


class CollabFilter:
    DOT = "dot"
    COSINE = "cosine"

    # name = 'collab_filter' or 'collab_filter_time'
    def __init__(self, name, **kwargs):
        embeddings = np.load(
            os.path.join(settings.ML_OUTPUT_PATH, name + "/embeddings.npz"),
            allow_pickle=True,
        )
        arr0, arr1 = embeddings.files
        self.name = name
        self.user_embeddings = embeddings[arr0]
        self.problem_embeddings = embeddings[arr1]

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

    def user_recommendations(self, user, problems, measure=DOT, limit=None, **kwargs):
        uid = user.id
        problems_hash = hashlib.sha1(str(list(problems)).encode()).hexdigest()
        cache_key = ":".join(map(str, [self.name, uid, measure, limit, problems_hash]))
        value = cache.get(cache_key)
        if value:
            return value

        if uid >= len(self.user_embeddings):
            uid = 0
        scores = self.compute_scores(
            self.user_embeddings[uid], self.problem_embeddings, measure
        )

        res = []  # [(score, problem)]
        for pid in problems:
            # pid = problem.id
            if pid < len(scores):
                res.append((scores[pid], pid))

        res.sort(reverse=True, key=lambda x: x[0])
        res = res[:limit]
        cache.set(cache_key, res, 3600)
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
