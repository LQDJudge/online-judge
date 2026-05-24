import hashlib
import importlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.db import connection

from judge.models import Problem

logger = logging.getLogger(__name__)

SEMANTIC_TABLE = "ml_problem_semantic_embedding"
SEMANTIC_ERROR_TABLE = "ml_problem_semantic_index_error"
DEFAULT_MODEL = "gemini-embedding-2"
DEFAULT_DIMS = 768
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
DEFAULT_REQUESTS_PER_MINUTE = 10
DEFAULT_BATCH_SIZE = 50
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_INITIAL_SLEEP = 5

_EMBEDDING_RATE_LIMIT_LOCK = threading.Lock()
_last_embedding_request_at = 0.0


class SemanticSearchUnavailable(Exception):
    pass


class SemanticIndexError(Exception):
    pass


class SemanticEmbeddingApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ProblemDocument:
    text: str
    pdf_bytes: bytes | None
    content_hash: str


def is_semantic_search_enabled():
    return bool(getattr(settings, "USE_ML", False))


def get_semantic_model():
    return getattr(settings, "SEMANTIC_SEARCH_MODEL", DEFAULT_MODEL)


def get_semantic_dims():
    return int(getattr(settings, "SEMANTIC_SEARCH_DIM", DEFAULT_DIMS))


def _get_query_cache_ttl():
    return int(getattr(settings, "SEMANTIC_SEARCH_QUERY_CACHE_TTL", 86400))


def get_embedding_requests_per_minute():
    return int(
        getattr(
            settings,
            "SEMANTIC_SEARCH_EMBEDDING_REQUESTS_PER_MINUTE",
            DEFAULT_REQUESTS_PER_MINUTE,
        )
    )


def get_embedding_batch_size():
    return int(
        getattr(
            settings,
            "SEMANTIC_SEARCH_EMBEDDING_BATCH_SIZE",
            DEFAULT_BATCH_SIZE,
        )
    )


def _get_max_retries():
    return int(
        getattr(settings, "SEMANTIC_SEARCH_EMBEDDING_MAX_RETRIES", DEFAULT_MAX_RETRIES)
    )


def _get_retry_initial_sleep():
    return float(
        getattr(
            settings,
            "SEMANTIC_SEARCH_EMBEDDING_RETRY_INITIAL_SLEEP",
            DEFAULT_RETRY_INITIAL_SLEEP,
        )
    )


def _get_api_key():
    key = getattr(settings, "GEMINI_API_KEY", None) or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SemanticSearchUnavailable("GEMINI_API_KEY is not configured")
    return key


def _get_genai_modules():
    try:
        genai = importlib.import_module("google.genai")
        types = importlib.import_module("google.genai.types")
    except ImportError as exc:
        raise SemanticSearchUnavailable(
            "google-genai is not installed. Install judge/ml/requirements.txt."
        ) from exc
    return genai, types


def _get_model_resource_name():
    model = get_semantic_model()
    return model if model.startswith("models/") else f"models/{model}"


def _normalize_query(query):
    return re.sub(r"\s+", " ", (query or "").strip())


def _extract_embedding_values(response):
    value_sets = _extract_embedding_value_sets(response)
    if value_sets:
        return value_sets[0]
    raise SemanticSearchUnavailable("Google embedding response did not contain values")


def _extract_embedding_value_sets(response):
    embeddings = getattr(response, "embeddings", None)
    if embeddings:
        value_sets = []
        for embedding in embeddings:
            values = getattr(embedding, "values", None)
            if values is None:
                raise SemanticSearchUnavailable(
                    "Google embedding response did not contain values"
                )
            value_sets.append([float(value) for value in values])
        return value_sets

    embedding = getattr(response, "embedding", None)
    if embedding is not None:
        values = getattr(embedding, "values", embedding)
        return [[float(value) for value in values]]

    if isinstance(response, dict):
        if response.get("embeddings"):
            return [
                [float(value) for value in embedding.get("values", embedding)]
                for embedding in response["embeddings"]
            ]
        if response.get("embedding"):
            embedding = response["embedding"]
            return [[float(value) for value in embedding.get("values", embedding)]]

    raise SemanticSearchUnavailable("Google embedding response did not contain values")


def _sleep_for_rate_limit(units=1):
    global _last_embedding_request_at

    requests_per_minute = get_embedding_requests_per_minute()
    if requests_per_minute <= 0:
        return

    min_interval = 60.0 * max(1, units) / requests_per_minute
    with _EMBEDDING_RATE_LIMIT_LOCK:
        now = time.monotonic()
        sleep_for = _last_embedding_request_at + min_interval - now
        if sleep_for > 0:
            time.sleep(sleep_for)
        _last_embedding_request_at = time.monotonic()


def _get_retry_after_seconds(exc):
    if isinstance(exc, urllib.error.HTTPError):
        retry_after = exc.headers.get("retry-after") or exc.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                return None

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                return None
    return None


def _is_retryable_embedding_error(exc):
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status_code in (429, 500, 502, 503, 504):
        return True

    message = str(exc).lower()
    return any(
        retryable in message
        for retryable in (
            "429",
            "resource_exhausted",
            "rate limit",
            "quota",
            "temporarily unavailable",
            "deadline exceeded",
            "internal error",
        )
    )


def _extract_error_text(exc):
    if isinstance(exc, urllib.error.HTTPError):
        try:
            return exc.read().decode("utf-8", errors="replace")
        except Exception:
            return str(exc)
    return str(exc)


def _make_batch_embed_request(texts):
    model_resource_name = _get_model_resource_name()
    return {
        "requests": [
            {
                "model": model_resource_name,
                "content": {"parts": [{"text": text}]},
                "taskType": "SEMANTIC_SIMILARITY",
                "outputDimensionality": get_semantic_dims(),
            }
            for text in texts
        ]
    }


def _batch_embed_texts_rest(texts):
    model_resource_name = _get_model_resource_name()
    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"{model_resource_name}:batchEmbedContents"
    )
    body = json.dumps(_make_batch_embed_request(texts)).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": _get_api_key(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SemanticEmbeddingApiError(_extract_error_text(exc), exc.code) from exc
    except urllib.error.URLError as exc:
        raise SemanticEmbeddingApiError(str(exc)) from exc

    value_sets = _extract_embedding_value_sets(data)
    if len(value_sets) != len(texts):
        raise SemanticSearchUnavailable(
            f"Embedding response had {len(value_sets)} vectors for {len(texts)} inputs"
        )
    for values in value_sets:
        if len(values) != get_semantic_dims():
            raise SemanticSearchUnavailable(
                f"Embedding had {len(values)} dimensions, expected {get_semantic_dims()}"
            )
    return value_sets


def is_embedding_quota_error(exc):
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status_code == 429:
        return True

    message = str(exc).lower()
    return any(
        quota_text in message
        for quota_text in ("429", "resource_exhausted", "rate limit", "quota")
    )


def _call_embed_content(client, model, content, config):
    max_retries = _get_max_retries()
    retry_sleep = _get_retry_initial_sleep()
    for attempt in range(max_retries + 1):
        _sleep_for_rate_limit()
        try:
            return client.models.embed_content(
                model=model,
                contents=content,
                config=config,
            )
        except Exception as exc:
            if attempt >= max_retries or not _is_retryable_embedding_error(exc):
                raise

            retry_after = _get_retry_after_seconds(exc)
            sleep_for = retry_after if retry_after is not None else retry_sleep
            logger.warning(
                "Embedding request failed with retryable error; retrying in %.1fs "
                "(attempt %d/%d): %s",
                sleep_for,
                attempt + 1,
                max_retries,
                exc,
            )
            time.sleep(sleep_for)
            retry_sleep = min(retry_sleep * 2, 300)


def _embed_content(content):
    return _embed_contents([content])[0]


def _embed_contents(contents):
    if all(isinstance(content, str) for content in contents):
        return _call_batch_embed_texts_rest(contents)

    genai, types = _get_genai_modules()
    client = genai.Client(api_key=_get_api_key())
    config = types.EmbedContentConfig(
        output_dimensionality=get_semantic_dims(),
        task_type="SEMANTIC_SIMILARITY",
    )
    response = _call_embed_content(client, get_semantic_model(), contents, config)
    value_sets = _extract_embedding_value_sets(response)
    if len(value_sets) != len(contents):
        raise SemanticSearchUnavailable(
            f"Embedding response had {len(value_sets)} vectors for {len(contents)} inputs"
        )
    for values in value_sets:
        if len(values) != get_semantic_dims():
            raise SemanticSearchUnavailable(
                f"Embedding had {len(values)} dimensions, expected {get_semantic_dims()}"
            )
    return value_sets


def _call_batch_embed_texts_rest(texts):
    max_retries = _get_max_retries()
    retry_sleep = _get_retry_initial_sleep()
    for attempt in range(max_retries + 1):
        _sleep_for_rate_limit(len(texts))
        try:
            return _batch_embed_texts_rest(texts)
        except Exception as exc:
            if attempt >= max_retries or not _is_retryable_embedding_error(exc):
                raise

            retry_after = _get_retry_after_seconds(exc)
            sleep_for = retry_after if retry_after is not None else retry_sleep
            logger.warning(
                "Batch embedding request failed with retryable error; retrying in %.1fs "
                "(attempt %d/%d): %s",
                sleep_for,
                attempt + 1,
                max_retries,
                exc,
            )
            time.sleep(sleep_for)
            retry_sleep = min(retry_sleep * 2, 300)


def _vector_to_text(values):
    return json.dumps([float(value) for value in values])


def embed_text_query(query):
    normalized = _normalize_query(query)
    if not normalized:
        raise ValueError("Query is empty")

    cache_key = "semantic_query_embedding:%s:%s:%s" % (
        get_semantic_model(),
        get_semantic_dims(),
        hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    vector = _vector_to_text(_embed_content(normalized))
    cache.set(cache_key, vector, _get_query_cache_ttl())
    return vector


def _read_pdf_bytes(problem):
    if not problem.pdf_description:
        return None
    storage_name = problem.pdf_description.name
    try:
        with default_storage.open(storage_name, "rb") as pdf_file:
            return pdf_file.read()
    except Exception as exc:
        storage_error = exc

    url = _get_media_url(storage_name)
    if url:
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "LQDOJ semantic indexer"}
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except Exception:
            pass

    raise SemanticIndexError(
        f"Could not read PDF statement: {storage_error}"
    ) from storage_error


def _get_media_url(storage_name):
    media_url = getattr(settings, "MEDIA_URL", "")
    if not media_url.startswith(("http://", "https://")):
        return None
    quoted_name = urllib.parse.quote(storage_name.lstrip("/"), safe="/")
    return urllib.parse.urljoin(media_url.rstrip("/") + "/", quoted_name)


def build_problem_document(problem):
    problem = (
        Problem.objects.select_related("group")
        .prefetch_related("types", "translations")
        .get(id=problem.id)
    )
    translations = list(problem.translations.all())
    types = list(problem.types.all())

    parts = [
        f"Code: {problem.code}",
        f"Name: {problem.name}",
    ]
    if problem.group:
        parts.append(f"Group: {problem.group.full_name}")
    if types:
        parts.append(
            "Types: " + ", ".join(problem_type.full_name for problem_type in types)
        )
    if problem.summary:
        parts.append("Summary:\n" + problem.summary)
    if problem.description:
        parts.append("Statement:\n" + problem.description)

    for translation in translations:
        parts.append(
            "Translation (%s):\nName: %s\nStatement:\n%s"
            % (translation.language, translation.name, translation.description)
        )

    pdf_bytes = _read_pdf_bytes(problem)
    hash_payload = {
        "code": problem.code,
        "name": problem.name,
        "group": problem.group.full_name if problem.group else "",
        "types": [problem_type.full_name for problem_type in types],
        "summary": problem.summary or "",
        "description": problem.description or "",
        "translations": [
            {
                "language": translation.language,
                "name": translation.name,
                "description": translation.description,
            }
            for translation in translations
        ],
        "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest() if pdf_bytes else "",
    }
    content_hash = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return ProblemDocument(
        text="\n\n".join(parts), pdf_bytes=pdf_bytes, content_hash=content_hash
    )


def _embed_problem_document(document):
    if document.pdf_bytes:
        genai, types = _get_genai_modules()
        content = types.Content(
            parts=[
                types.Part.from_text(text=document.text),
                types.Part.from_bytes(
                    data=document.pdf_bytes, mime_type="application/pdf"
                ),
            ]
        )
        return _vector_to_text(_embed_content(content))
    return _vector_to_text(_embed_content(document.text))


def is_problem_searchable(problem):
    return bool(problem.is_public and not problem.is_organization_private)


def prune_problem_embedding(problem_id):
    with connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {SEMANTIC_TABLE} WHERE problem_id = %s", [problem_id]
        )
        cursor.execute(
            f"DELETE FROM {SEMANTIC_ERROR_TABLE} WHERE problem_id = %s", [problem_id]
        )


def _existing_content_hash(problem_id):
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT content_hash
            FROM {SEMANTIC_TABLE}
            WHERE problem_id = %s AND model = %s AND dims = %s
            """,
            [problem_id, get_semantic_model(), get_semantic_dims()],
        )
        row = cursor.fetchone()
    return row[0] if row else None


def _error_content_hash(problem_id):
    payload = "%s:%s:%s:index-error" % (
        problem_id,
        get_semantic_model(),
        get_semantic_dims(),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_index_error(problem_id, content_hash, error_text):
    content_hash = content_hash or _error_content_hash(problem_id)
    with connection.cursor() as cursor:
        cursor.execute(
            f"DELETE FROM {SEMANTIC_TABLE} WHERE problem_id = %s", [problem_id]
        )
        cursor.execute(
            f"""
            INSERT INTO {SEMANTIC_ERROR_TABLE}
                (problem_id, model, dims, content_hash, error_text, failed_at)
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON DUPLICATE KEY UPDATE
                model = VALUES(model),
                dims = VALUES(dims),
                content_hash = VALUES(content_hash),
                error_text = VALUES(error_text),
                failed_at = CURRENT_TIMESTAMP
            """,
            [
                problem_id,
                get_semantic_model(),
                get_semantic_dims(),
                content_hash,
                error_text[:4000],
            ],
        )


def _upsert_embedding(problem_id, content_hash, vector_text):
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO {SEMANTIC_TABLE}
                (problem_id, model, dims, content_hash, embedding, indexed_at)
            VALUES (%s, %s, %s, %s, Vec_FromText(%s), CURRENT_TIMESTAMP)
            ON DUPLICATE KEY UPDATE
                model = VALUES(model),
                dims = VALUES(dims),
                content_hash = VALUES(content_hash),
                embedding = VALUES(embedding),
                indexed_at = CURRENT_TIMESTAMP
            """,
            [
                problem_id,
                get_semantic_model(),
                get_semantic_dims(),
                content_hash,
                vector_text,
            ],
        )
        cursor.execute(
            f"DELETE FROM {SEMANTIC_ERROR_TABLE} WHERE problem_id = %s", [problem_id]
        )


def index_problem_embedding(problem, force=False):
    if not is_semantic_search_enabled():
        return {"indexed": False, "skipped": True, "reason": "USE_ML is disabled"}

    if not hasattr(problem, "id"):
        problem = Problem.objects.get(id=problem)

    if not is_problem_searchable(problem):
        prune_problem_embedding(problem.id)
        return {
            "indexed": False,
            "pruned": True,
            "reason": "problem is not public-searchable",
        }

    document = None
    try:
        document = build_problem_document(problem)
        if not force and _existing_content_hash(problem.id) == document.content_hash:
            return {
                "indexed": False,
                "skipped": True,
                "reason": "content hash unchanged",
            }

        vector_text = _embed_problem_document(document)
        _upsert_embedding(problem.id, document.content_hash, vector_text)
    except Exception as exc:
        _record_index_error(
            problem.id, getattr(document, "content_hash", None), str(exc)
        )
        raise

    return {"indexed": True, "content_hash": document.content_hash}


def prune_stale_embeddings():
    with connection.cursor() as cursor:
        cursor.execute(f"""
            DELETE e FROM {SEMANTIC_TABLE} e
            LEFT JOIN judge_problem p ON p.id = e.problem_id
            WHERE p.id IS NULL OR p.is_public = 0 OR p.is_organization_private = 1
            """)
        embeddings_deleted = cursor.rowcount
        cursor.execute(f"""
            DELETE e FROM {SEMANTIC_ERROR_TABLE} e
            LEFT JOIN judge_problem p ON p.id = e.problem_id
            WHERE p.id IS NULL OR p.is_public = 0 OR p.is_organization_private = 1
            """)
        errors_deleted = cursor.rowcount
    return {"embeddings_deleted": embeddings_deleted, "errors_deleted": errors_deleted}


def _semantic_vector_search(query_vec, limit, exclude_id=None):
    exclude_clause = ""
    params = [query_vec, get_semantic_model(), get_semantic_dims()]
    if exclude_id is not None:
        exclude_clause = "AND e.problem_id != %s"
        params.append(exclude_id)

    sql = f"""
        SELECT e.problem_id,
               (1 - VEC_DISTANCE_COSINE(e.embedding, Vec_FromText(%s))) AS score
        FROM {SEMANTIC_TABLE} e
        INNER JOIN judge_problem p ON p.id = e.problem_id
        WHERE e.model = %s
          AND e.dims = %s
          AND p.is_public = 1
          AND p.is_organization_private = 0
          {exclude_clause}
        ORDER BY VEC_DISTANCE_COSINE(e.embedding, Vec_FromText(%s))
        LIMIT %s
    """
    params += [query_vec, limit]
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return [
            (int(problem_id), float(score)) for problem_id, score in cursor.fetchall()
        ]


def _format_results(rows):
    ids = [problem_id for problem_id, score in rows]
    problem_map = {
        problem.id: problem for problem in Problem.get_cached_instances(*ids)
    }
    results = []
    for problem_id, score in rows:
        problem = problem_map.get(problem_id)
        if not problem:
            continue
        results.append(
            {
                "code": problem.get_code(),
                "name": problem.get_name(),
                "url": problem.get_absolute_url(),
                "points": problem.get_points(),
                "group": problem.get_group_name(),
                "score": score,
            }
        )
    return results


def clamp_limit(limit):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def search_problems(query, limit=DEFAULT_LIMIT):
    query_vec = embed_text_query(query)
    rows = _semantic_vector_search(query_vec, clamp_limit(limit))
    return _format_results(rows)


def _stored_problem_embedding(problem_id):
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT Vec_ToText(embedding)
            FROM {SEMANTIC_TABLE}
            WHERE problem_id = %s AND model = %s AND dims = %s
            """,
            [problem_id, get_semantic_model(), get_semantic_dims()],
        )
        row = cursor.fetchone()
    return row[0] if row else None


def similar_problems(problem, limit=DEFAULT_LIMIT):
    if not hasattr(problem, "id"):
        problem = Problem.objects.get(code=problem)

    query_vec = _stored_problem_embedding(problem.id)
    if query_vec is None:
        document = build_problem_document(problem)
        query_vec = _embed_problem_document(document)

    rows = _semantic_vector_search(query_vec, clamp_limit(limit), exclude_id=problem.id)
    return _format_results(rows)
