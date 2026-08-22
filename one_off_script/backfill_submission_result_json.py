#!/usr/bin/env python3
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dmoj.settings")

import django  # noqa: E402

django.setup()

from django.core.files.storage import default_storage  # noqa: E402
from django.db import close_old_connections  # noqa: E402

from judge.models import Submission, SubmissionTestCase  # noqa: E402
from judge.utils.submission_results import (  # noqa: E402
    submission_result_content,
    submission_result_path,
)
from judge.views.submission import get_cases_data  # noqa: E402

S3_CLIENT = None


def parse_args():
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Backfill submission result.json objects from existing testcase rows.",
    )
    parser.add_argument("--start-id", type=int, default=0)
    parser.add_argument("--end-id", type=int)
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--s3-workers", type=int, default=32)
    parser.add_argument("--include-feedback", action="store_true")
    parser.add_argument("--include-previews", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def log(message, *args):
    if args:
        message = message % args
    print(message, flush=True)


def testcase_details_with_previews(submission, include_feedback):
    cases_data = get_cases_data(submission)
    details = []
    for case in sorted(submission.test_cases.all(), key=lambda item: item.case):
        preview = cases_data.get(case.case)
        detail = {
            "case": case.case,
            "output": case.output,
        }
        if include_feedback:
            detail.update(
                {
                    "feedback": case.feedback,
                    "extended_feedback": case.extended_feedback,
                }
            )
        if preview is not None:
            detail.update(
                {
                    "input": preview.get("input", ""),
                    "input_available": True,
                    "answer": preview.get("answer", ""),
                    "answer_available": True,
                }
            )
        else:
            detail.update(
                {
                    "input": "",
                    "input_available": False,
                    "answer": "",
                    "answer_available": False,
                }
            )
        details.append(detail)
    return details


def batch_details_from_rows(ids, include_feedback):
    details_by_submission = {submission_id: [] for submission_id in ids}
    fields = ["submission_id", "case", "output"]
    if include_feedback:
        fields += ["feedback", "extended_feedback"]

    rows = (
        SubmissionTestCase.objects.filter(submission_id__in=ids)
        .order_by("submission_id", "case")
        .values_list(*fields)
        .iterator(chunk_size=10000)
    )
    for row in rows:
        if include_feedback:
            submission_id, case, output, feedback, extended_feedback = row
        else:
            submission_id, case, output = row
            feedback = ""
            extended_feedback = ""

        detail = {"case": case, "output": output or ""}
        if include_feedback:
            detail.update(
                {
                    "feedback": feedback or "",
                    "extended_feedback": extended_feedback or "",
                }
            )
        details_by_submission[submission_id].append(detail)
    return details_by_submission


def batch_details_with_previews(ids, include_feedback):
    submissions = (
        Submission.objects.filter(id__in=ids)
        .select_related("problem", "problem__data_files")
        .prefetch_related("test_cases")
    )
    by_id = {submission.id: submission for submission in submissions}
    details_by_submission = {}
    for submission_id in ids:
        submission = by_id.get(submission_id)
        if submission is None:
            continue
        details_by_submission[submission_id] = testcase_details_with_previews(
            submission, include_feedback
        )
    return details_by_submission


def batch_details(ids, include_feedback, include_previews):
    if include_previews:
        return batch_details_with_previews(ids, include_feedback)
    return batch_details_from_rows(ids, include_feedback)


def s3_object_key(path):
    location = (getattr(default_storage, "location", "") or "").strip("/")
    return "%s/%s" % (location, path) if location else path


def require_s3_storage():
    if not hasattr(default_storage, "connection") or not hasattr(
        default_storage, "bucket_name"
    ):
        raise RuntimeError("backfill requires an S3-compatible default_storage")


def configure_s3_client(max_pool_connections):
    global S3_CLIENT

    require_s3_storage()
    from botocore.config import Config

    client_config = getattr(default_storage, "client_config", None)
    pool_config = Config(max_pool_connections=max_pool_connections)
    if client_config is not None:
        pool_config = client_config.merge(pool_config)

    session = default_storage._create_session()
    S3_CLIENT = session.client(
        "s3",
        region_name=getattr(default_storage, "region_name", None),
        use_ssl=getattr(default_storage, "use_ssl", True),
        endpoint_url=getattr(default_storage, "endpoint_url", None),
        config=pool_config,
        verify=getattr(default_storage, "verify", None),
    )


def s3_client():
    if S3_CLIENT is None:
        raise RuntimeError("S3 client is not configured")
    return S3_CLIENT


def s3_write_parameters(path):
    key = s3_object_key(path)
    if hasattr(default_storage, "_get_write_parameters"):
        params = default_storage._get_write_parameters(key, None)
    else:
        params = dict(getattr(default_storage, "object_parameters", {}) or {})
    params["ContentType"] = "application/json"
    return key, params


def s3_result_exists(submission_id):
    from botocore.exceptions import ClientError

    path = submission_result_path(submission_id)
    try:
        s3_client().head_object(
            Bucket=default_storage.bucket_name,
            Key=s3_object_key(path),
        )
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            return False
        raise
    return True


def s3_put_submission_result(submission_id, details):
    path = submission_result_path(submission_id)
    key, params = s3_write_parameters(path)
    params.update(
        {
            "Bucket": default_storage.bucket_name,
            "Key": key,
            "Body": submission_result_content(details, compact=True),
        }
    )
    s3_client().put_object(**params)
    return path


def write_submission_result(submission_id, details, skip_existing):
    if skip_existing and s3_result_exists(submission_id):
        return "skipped"
    s3_put_submission_result(submission_id, details)
    return "written"


def submission_batches(start_id, end_id, batch_size):
    last_id = start_id
    while True:
        qs = Submission.objects.filter(id__gt=last_id).order_by("id")
        if end_id is not None:
            qs = qs.filter(id__lte=end_id)
        ids = list(qs.values_list("id", flat=True)[:batch_size])
        if not ids:
            return
        yield ids
        last_id = ids[-1]


def main():
    args = parse_args()
    processed = 0
    written = 0
    skipped = 0
    failed = 0
    batches = 0
    started_at = time.monotonic()

    log(
        "starting submission result backfill start_id=%s end_id=%s batch_size=%s s3_workers=%s include_feedback=%s include_previews=%s skip_existing=%s dry_run=%s storage=%s",
        args.start_id,
        args.end_id,
        args.batch_size,
        args.s3_workers,
        args.include_feedback,
        args.include_previews,
        args.skip_existing,
        args.dry_run,
        default_storage.__class__.__name__,
    )

    if not args.dry_run:
        configure_s3_client(args.s3_workers)

    with ThreadPoolExecutor(max_workers=args.s3_workers) as executor:
        for ids in submission_batches(args.start_id, args.end_id, args.batch_size):
            batches += 1
            batch_started_at = time.monotonic()
            log(
                "batch start batch=%d count=%d first_id=%d last_id=%d",
                batches,
                len(ids),
                ids[0],
                ids[-1],
            )
            close_old_connections()
            details_by_submission = batch_details(
                ids, args.include_feedback, args.include_previews
            )
            futures = {}
            for submission_id in ids:
                processed += 1
                details = details_by_submission.get(submission_id)
                if details is None:
                    skipped += 1
                    continue
                if args.dry_run:
                    written += 1
                    continue
                future = executor.submit(
                    write_submission_result,
                    submission_id,
                    details,
                    args.skip_existing,
                )
                futures[future] = submission_id

            for future in as_completed(futures):
                submission_id = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    failed += 1
                    print(
                        "failed submission=%d error=%r" % (submission_id, exc),
                        file=sys.stderr,
                    )
                    continue
                if result == "skipped":
                    skipped += 1
                else:
                    written += 1

            batch_seconds = time.monotonic() - batch_started_at
            total_seconds = time.monotonic() - started_at
            log(
                "progress processed=%d written=%d skipped=%d failed=%d last_id=%d batch_seconds=%.2f batch_rate=%.2f/s total_rate=%.2f/s"
                % (
                    processed,
                    written,
                    skipped,
                    failed,
                    ids[-1],
                    batch_seconds,
                    len(ids) / batch_seconds if batch_seconds else 0,
                    processed / total_seconds if total_seconds else 0,
                ),
            )

    log(
        "done batches=%d processed=%d written=%d skipped=%d failed=%d",
        batches,
        processed,
        written,
        skipped,
        failed,
    )
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
