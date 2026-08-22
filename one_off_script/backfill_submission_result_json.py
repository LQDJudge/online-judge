#!/usr/bin/env python3
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dmoj.settings")

import django  # noqa: E402

django.setup()

from django.db import close_old_connections  # noqa: E402

from judge.models import Submission  # noqa: E402
from judge.utils.submission_results import (  # noqa: E402
    save_submission_result,
    submission_result_exists,
)
from judge.views.submission import get_cases_data  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(
        description="Backfill submission result.json objects from existing testcase rows."
    )
    parser.add_argument("--start-id", type=int, default=0)
    parser.add_argument("--end-id", type=int)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def testcase_details(submission):
    cases_data = get_cases_data(submission)
    details = []
    for case in sorted(submission.test_cases.all(), key=lambda item: item.case):
        preview = cases_data.get(case.case)
        detail = {
            "case": case.case,
            "output": case.output,
            "feedback": case.feedback,
            "extended_feedback": case.extended_feedback,
        }
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

    for ids in submission_batches(args.start_id, args.end_id, args.batch_size):
        close_old_connections()
        submissions = (
            Submission.objects.filter(id__in=ids)
            .select_related("problem", "problem__data_files")
            .prefetch_related("test_cases")
        )
        by_id = {submission.id: submission for submission in submissions}
        for submission_id in ids:
            processed += 1
            submission = by_id.get(submission_id)
            if submission is None:
                skipped += 1
                continue
            if args.skip_existing and submission_result_exists(submission.id):
                skipped += 1
                continue

            try:
                details = testcase_details(submission)
                if args.dry_run:
                    print(
                        "would write submission=%d cases=%d"
                        % (submission.id, len(details))
                    )
                else:
                    save_submission_result(submission.id, details)
                written += 1
            except Exception as exc:
                failed += 1
                print(
                    "failed submission=%d error=%r" % (submission.id, exc),
                    file=sys.stderr,
                )

        print(
            "progress processed=%d written=%d skipped=%d failed=%d last_id=%d"
            % (processed, written, skipped, failed, ids[-1]),
            flush=True,
        )

    if failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
