"""
VectorStore: MariaDB-backed embedding storage and similarity search.
Uses VEC_DISTANCE_COSINE() for similarity computation.
Temporary raw SQL until Django adds vector ORM support.

Strategy selection based on allowed set size:
- Small sets (< _BRUTE_FORCE_THRESHOLD): WHERE IN brute-force is faster
  because scanning a small number of rows beats HNSW overhead.
- Large sets: HNSW index scan (O(log n)) with overfetch + Python filter,
  since MariaDB can only use the HNSW index without a WHERE clause.
"""

import logging

from django.db import connection

logger = logging.getLogger(__name__)

TABLE_MAP = {
    "collab_filter": {
        "problem": "ml_problem_embedding_cf",
        "user": "ml_user_embedding_cf",
    },
    "collab_filter_time": {
        "problem": "ml_problem_embedding_cf_time",
        "user": "ml_user_embedding_cf_time",
    },
    "two_tower": {
        "problem": "ml_problem_embedding_tt",
        "user": "ml_user_embedding_tt",
    },
}

# Below this threshold, WHERE IN brute-force is faster than HNSW overfetch.
# Benchmarked: WHERE IN wins at n<=200, HNSW wins at n>=500.
_BRUTE_FORCE_THRESHOLD = 500

# How many rows to fetch from the HNSW index before filtering.
_OVERFETCH = 500


def _get_embedding(table, id_col, entity_id, fallback_id=None):
    """Fetch an embedding vector as text. Optionally falls back to fallback_id."""
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT Vec_ToText(embedding) FROM {table} WHERE {id_col} = %s",
            [entity_id],
        )
        row = cursor.fetchone()
        if row:
            return row[0]
        if fallback_id is not None:
            cursor.execute(
                f"SELECT Vec_ToText(embedding) FROM {table} WHERE {id_col} = %s",
                [fallback_id],
            )
            row = cursor.fetchone()
            return row[0] if row else None
        return None


class VectorStore:
    def __init__(self, name):
        if name not in TABLE_MAP:
            raise ValueError(f"Unknown model: {name}")
        self.name = name
        self.problem_table = TABLE_MAP[name]["problem"]
        self.user_table = TABLE_MAP[name]["user"]

    def __str__(self):
        return self.name

    def _brute_force_search(self, table, id_col, query_vec, id_list, limit):
        """WHERE IN brute-force search. Fast for small candidate sets."""
        placeholders = ",".join(["%s"] * len(id_list))
        sql = f"""
            SELECT {id_col},
                   (1 - VEC_DISTANCE_COSINE(embedding, Vec_FromText(%s))) AS score
            FROM {table}
            WHERE {id_col} IN ({placeholders})
            ORDER BY score DESC
            LIMIT %s
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, [query_vec] + id_list + [limit])
            return [(float(score), eid) for eid, score in cursor.fetchall()]

    def _hnsw_search(self, table, id_col, query_vec, allowed_set, limit):
        """HNSW index scan with overfetch + Python filter. Fast for large sets."""
        overfetch = max(_OVERFETCH, limit * 5)
        sql = f"""
            SELECT {id_col},
                   (1 - VEC_DISTANCE_COSINE(embedding, Vec_FromText(%s))) AS score
            FROM {table}
            ORDER BY VEC_DISTANCE_COSINE(embedding, Vec_FromText(%s))
            LIMIT %s
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, [query_vec, query_vec, overfetch])
            rows = cursor.fetchall()

        results = [(float(score), eid) for eid, score in rows if eid in allowed_set]

        if len(results) >= limit:
            return results[:limit]

        # Fallback: HNSW didn't cover enough of the allowed set
        return self._brute_force_search(
            table, id_col, query_vec, list(allowed_set), limit
        )

    def _vector_search(self, table, id_col, query_vec, allowed_ids, exclude_id, limit):
        """
        Pick the fastest strategy based on candidate set size:
        - Small sets: brute-force WHERE IN
        - Large sets: HNSW index with overfetch + Python filter
        """
        if not allowed_ids:
            return []
        effective_limit = limit or len(allowed_ids)
        allowed_set = set(allowed_ids)
        if exclude_id is not None:
            allowed_set.discard(exclude_id)

        if len(allowed_set) < _BRUTE_FORCE_THRESHOLD:
            return self._brute_force_search(
                table, id_col, query_vec, list(allowed_set), effective_limit
            )
        return self._hnsw_search(table, id_col, query_vec, allowed_set, effective_limit)

    def user_recommendations(self, user_id, problems, limit):
        """
        Return list of (score, problem_id), sorted by similarity desc.
        Score = 1 - cosine_distance (higher = more similar).
        """
        user_vec = _get_embedding(self.user_table, "user_id", user_id, fallback_id=0)
        if not user_vec:
            return []
        return self._vector_search(
            self.problem_table, "problem_id", user_vec, problems, None, limit
        )

    def problem_neighbors(self, problem, problemset, limit):
        """
        Return list of (score, problem_id) for problems similar to `problem`.
        """
        pid = problem.id if hasattr(problem, "id") else problem
        problem_vec = _get_embedding(self.problem_table, "problem_id", pid)
        if not problem_vec:
            return []
        return self._vector_search(
            self.problem_table, "problem_id", problem_vec, problemset, pid, limit
        )
