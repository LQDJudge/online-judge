# Quiz deadline enforcement and expiry worker

New attempts snapshot the earlier of their quiz-duration deadline and contest
participation deadline. Existing snapshots do not change when quiz/contest settings
are edited. There is no active-attempt extension/shortening UI in this change.
At server time `>= deadline`, student answer mutations are rejected. Only previously
saved answers are finalized. A request is evaluated after obtaining the attempt lock;
clicking before the deadline does not guarantee a request arriving later is accepted.

`end_time` remains the actual finalization processing timestamp.
`effective_end_time` is `min(deadline_at, captured_now)` (or captured_now if untimed).
Contest elapsed time and attempt duration prefer effective_end_time, with unchanged
legacy fallback to end_time. Worker delays do not grant extra answering time.

## Deploy

1. Back up the database and deploy the schema-only migration
   `0275_quiz_attempt_deadlines`. It adds nullable timestamps, a snapshot flag and
   an overdue-lookup index; it does not regrade or finalize historical attempts.
2. Restart site/Celery workers (and bridge processes using these Django models).
   Publish updated quiz.js and compiled Vietnamese catalogs using the normal static
   deployment workflow. No SCSS compilation is required by this change.
3. Keep `QUIZ_EXPIRY_ENABLED = False` initially (the default). Confirm new timed
   attempts have deadline_initialized=True and deadline_at populated.
4. Preview candidates with `python3 manage.py expire_quiz_attempts --dry-run`.
   This reports counts, oldest overdue age and up to 100 candidate IDs; it does not
   finalize attempts or advance the operational cursor.
5. After checking scope and obtaining production approval, set
   `QUIZ_EXPIRY_ENABLED = True` in local settings. Ensure both Celery beat and workers
   load the updated configuration/tasks. Beat schedules the task every 30 seconds.
6. Inspect worker output for overdue age, finalized/failed/skipped counts. A healthy
   worker usually finalizes within 30 seconds plus queue/processing delay, not exactly
   at the deadline. Any endpoint remains deadline-enforced during worker downtime.

The command without `--dry-run` writes progress/results and must not be run on
production without explicit scope approval. Batch size defaults to 100 (configurable
through QUIZ_EXPIRY_BATCH_SIZE), with a maximum of 1000 and a soft 20-second iteration
budget. An individual database/grading operation can exceed that budget; monitor
failures/latency and tune worker/database timeouts operationally. MariaDB must support
SELECT FOR UPDATE SKIP LOCKED (MariaDB 10.6+). Locked attempts are skipped and retried.
A rotating deadline/ID cache cursor prevents one failing record starving later work.
The cursor is only operational; row locking/idempotency provides correctness even if
the cache lock expires or the cursor is lost. Grading/result DB failures roll back
the attempt for retry. Post-commit notification failures are logged, without undoing
the grade; notifications are not a durable delivery guarantee.

## Legacy data and rollback

Legacy rows receive deadline_initialized=False and null new timestamps. The sweep
ignores them; normal user requests still derive their old deadline for enforcement.
No mass finalization, penalty correction, source answer rewrite or score backfill is
part of deployment. Historical fixes require a fresh audit, timing-policy review,
frozen target IDs, and separate production approval. Never infer a late answer solely
from historical answered_at: some older grading paths updated that auto_now field.

Disable QUIZ_EXPIRY_ENABLED to pause unattended processing. Do not remove the new
columns or clear deadlines during rollback; retain evidence and migration compatibility.
Code rollback to the previous release also restores its late-answer acceptance bug.

## Browser behavior and limitations

Autosaves are serialized per question within a page, pending edits display Saving,
and failures remain visible. Manual POST carries the current full form and cancels
queued autosaves. Status is polled every 30 seconds and on returning to the page.
The server can finish attempts after the browser closes; it cannot recover answers
that never reached the server. Cross-tab simultaneous editing is not conflict-merged;
the existing session restrictions still apply, and database locks enforce finalization.

## Development

Tests: `python3 manage.py test --keepdb judge.tests.test_quiz_deadlines` and
`node judge/tests/test_quiz_deadlines.js`, plus quiz/course/contest regression suites.
Browser testing is separate and was not requested for this implementation.

The separate Multiple True/False feature was stashed before this work. Its generated
migration also used number 0275; reconcile that migration dependency and overlapping
quiz grading/view/JS hunks when restoring the feature. Do not blindly pop the stash
over this change or deploy both migration leaves without reconciliation.
