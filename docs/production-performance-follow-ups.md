# Production performance follow-ups

This document records measured production work that is intentionally deferred from the
first performance pass. The evidence was collected on September 8-9, 2026. Recheck the
live metrics before implementation because traffic and index counters are time-dependent.

## Course grade calculation

`CourseStudentResults` and `CourseStudentResultsLesson` currently load every student and
calculate detailed lesson, contest, problem, and quiz results before filtering and
paginating. Measured large courses had 1,196-1,285 students while a page displays only 20,
and sampled requests made roughly 2,600-3,700 cache calls.

Planned change:

1. Query compact student identifiers and the values needed for filtering.
2. Calculate only compact total scores and global ranks for the complete student set.
3. Apply search, organization, friend, focus, and page selection.
4. Calculate detailed lesson/contest/problem/quiz breakdowns only for students on the
   selected page.
5. Preserve exact rank, tie, filter, and displayed-grade semantics with equivalence tests.

Do not add a long-lived grade cache initially. First remove the unnecessary object graph
and measure CPU, memory, database queries, and cache calls again.

## Duplicate index cleanup

MariaDB `userstat` identified eight exact duplicate index pairs. Remove only the redundant
explicit `Meta.indexes` copies through Django model changes and migrations; retain unique
constraints and automatic field indexes.

Planned cleanup areas:

- `BestQuizAttempt`: explicit `(user, lesson_quiz)` duplicates its unique constraint.
- `BestSubmission`: explicit `(user, problem)` duplicates its unique constraint.
- `Bookmark`: explicit `(content_type, object_id)` duplicates its unique constraint.
- `PageVote`: explicit `(content_type, object_id)` duplicates its unique constraint.
- `Notification.time`: explicit index duplicates `db_index=True`.
- `RequestMetric.time`: explicit index duplicates `db_index=True`.
- `RequestMetric.response_time_ms`: retain one of the two exact indexes.
- `Room.organization_id_snapshot`: retain one of the two exact indexes.

Before deployment, inspect generated migration SQL and verify the retained index or unique
constraint for every pair. Do not remove other zero-read indexes based only on the current
short observation window; collect at least 30-60 days of representative usage and check
foreign keys, rare jobs, admin paths, and query plans first.
