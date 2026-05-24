from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from judge.ml.semantic_search import (
    _embed_contents,
    _embed_problem_document,
    _existing_content_hash,
    _record_index_error,
    _upsert_embedding,
    _vector_to_text,
    build_problem_document,
    get_embedding_batch_size,
    index_problem_embedding,
    is_embedding_quota_error,
    is_problem_searchable,
    prune_problem_embedding,
    prune_stale_embeddings,
)
from judge.models import Problem


class Command(BaseCommand):
    help = "Index public problem semantic embeddings for internal semantic search"

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument(
            "--all", action="store_true", help="Index all public problems"
        )
        target.add_argument(
            "--problem", type=str, help="Index or prune one problem code"
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-index even if content hash is unchanged",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Remove stale private/deleted embeddings before indexing",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=None,
            help="Text-only embedding batch size. Defaults to SEMANTIC_SEARCH_EMBEDDING_BATCH_SIZE.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of problems to prepare/index in this run.",
        )

    def handle(self, *args, **options):
        if not getattr(settings, "USE_ML", False):
            raise CommandError(
                "USE_ML must be True to index semantic problem embeddings"
            )

        if options["prune"]:
            result = prune_stale_embeddings()
            self.stdout.write(
                "Pruned %(embeddings_deleted)s embeddings and %(errors_deleted)s errors"
                % result
            )

        if options["problem"]:
            self._index_one(options["problem"], options["force"])
            return

        self._index_all(options["force"], options["batch_size"], options["limit"])

    def _index_one(self, code, force):
        try:
            problem = Problem.objects.only(
                "id", "code", "is_public", "is_organization_private"
            ).get(code=code)
        except Problem.DoesNotExist as exc:
            raise CommandError(f"Problem not found: {code}") from exc

        try:
            result = index_problem_embedding(problem, force=force)
        except Exception as exc:
            raise CommandError(f"Failed to index {code}: {exc}") from exc

        self.stdout.write(self.style.SUCCESS(f"{code}: {result}"))

    def _index_all(self, force, batch_size, limit):
        queryset = Problem.get_public_problems().only(
            "id", "code", "is_public", "is_organization_private"
        )
        total = queryset.count()
        batch_size = batch_size or get_embedding_batch_size()
        indexed = skipped = failed = pruned = prepared = 0
        text_batch = []

        def flush_text_batch():
            nonlocal indexed, failed
            if not text_batch:
                return

            batch = list(text_batch)
            text_batch.clear()
            try:
                vectors = _embed_contents([document.text for _, document in batch])
            except Exception as exc:
                if is_embedding_quota_error(exc):
                    raise CommandError(
                        "Google embedding quota/rate limit reached; stopping index run. "
                        "Retry later or lower SEMANTIC_SEARCH_EMBEDDING_REQUESTS_PER_MINUTE. "
                        f"Original error: {exc}"
                    ) from exc
                failed += len(batch)
                for problem, document in batch:
                    _record_index_error(problem.id, document.content_hash, str(exc))
                    self.stdout.write(
                        self.style.ERROR(
                            f"{problem.code}: batch embedding failed: {exc}"
                        )
                    )
                return

            for (problem, document), vector in zip(batch, vectors):
                _upsert_embedding(
                    problem.id, document.content_hash, _vector_to_text(vector)
                )
                indexed += 1
                self.stdout.write(f"{problem.code}: indexed")

        for offset, problem in enumerate(queryset.iterator(), start=1):
            if limit is not None and prepared >= limit:
                break
            document = None
            try:
                if not is_problem_searchable(problem):
                    prune_problem_embedding(problem.id)
                    pruned += 1
                    self.stdout.write(f"[{offset}/{total}] {problem.code}: pruned")
                    continue

                document = build_problem_document(problem)
                if (
                    not force
                    and _existing_content_hash(problem.id) == document.content_hash
                ):
                    skipped += 1
                    self.stdout.write(
                        f"[{offset}/{total}] {problem.code}: skipped (content hash unchanged)"
                    )
                    continue

                prepared += 1
                if document.pdf_bytes:
                    flush_text_batch()
                    try:
                        vector = _embed_problem_document(document)
                    except Exception as exc:
                        if is_embedding_quota_error(exc):
                            raise CommandError(
                                "Google embedding quota/rate limit reached; stopping index run. "
                                "Retry later or lower SEMANTIC_SEARCH_EMBEDDING_REQUESTS_PER_MINUTE. "
                                f"Original error: {exc}"
                            ) from exc
                        raise
                    _upsert_embedding(problem.id, document.content_hash, vector)
                    indexed += 1
                    self.stdout.write(
                        f"[{offset}/{total}] {problem.code}: indexed (PDF)"
                    )
                else:
                    text_batch.append((problem, document))
                    if len(text_batch) >= batch_size:
                        self.stdout.write(
                            f"[{offset}/{total}] embedding text batch of {len(text_batch)} problems"
                        )
                        flush_text_batch()
            except CommandError:
                raise
            except Exception as exc:
                failed += 1
                _record_index_error(
                    problem.id,
                    getattr(document, "content_hash", None),
                    str(exc),
                )
                self.stdout.write(
                    self.style.ERROR(f"[{offset}/{total}] {problem.code}: {exc}")
                )

        flush_text_batch()
        self.stdout.write(
            self.style.SUCCESS(
                "Done. total=%d, prepared=%d, indexed=%d, skipped=%d, pruned=%d, failed=%d"
                % (total, prepared, indexed, skipped, pruned, failed)
            )
        )
