# Kaggle-Like Problems on LQDOJ — Design

**Date:** 2026-04-26
**Status:** Design draft
**Goal:** Make it dramatically easier to (a) author and (b) submit "Kaggle-style" problems on LQDOJ — i.e. problems where the user downloads an input dataset, computes predictions/outputs locally, and uploads a single result file to be scored against a hidden answer using a custom scoring metric. *No model training on our servers* (no GPU).

---

## 1. Current State (what we already have)

LQDOJ already has nearly all the primitives. Authoring an output-only problem today is:
1. Tick `output_only` in the problem-data admin form (`judge/views/problem_data.py:113`).
2. Upload a zip with the test inputs + expected outputs (problem_data already auto-generates `init.yml` from the form — no manual YAML).
3. Upload a custom checker file (`custom_checker` Python or `custom_checker_cpp`) implementing the scoring metric.
4. Upload the **training/input dataset** to the author's personal file area (`judge/views/custom_file_upload.py`, path `user_uploads/<username>/`) and link to it from the statement markdown.

So the friction is **not** YAML, and **not** packaging the test data. The real pain points are:

| Pain point | Where | Why it hurts for Kaggle-like problems |
|---|---|---|
| Training/input dataset lives in author's personal uploads | `custom_file_upload.py`; linked from statement | Detached from the problem — no first-class "Download dataset" button, no per-problem storage quota, breaks if author leaves, can't be migrated with the problem |
| Author must hand-write a checker for every metric | `ProblemData.custom_checker*`, run by judge | Most Kaggle problems use 1 of ~6 standard metrics (accuracy, RMSE, MAE, F1, AUC, log-loss). Re-implementing them — correctly, with edge cases, fast on big files — is the bulk of authoring work. |
| Submit form is generic | `ProblemSubmitForm` (`judge/forms.py:203`), `templates/problem/submit.html` | 10 MB hard cap (`judge/forms.py:198`); browser-side JSZip wrapping is opaque; multipart upload proxied through Django; presigned-upload path (`judge/views/direct_upload.py`) is not wired in for submissions |

**Already covered (no work needed):**
- **Leaderboard:** `judge/views/ranked_submission.py:23` (`RankedSubmissions`, "Best solutions for X") orders by `case_points` desc — per-problem leaderboard exists today.
- **Submission cap & rate limit:** in-contest hosting gives `ContestProblem.max_submissions` (`judge/models/contest.py:920`) + `Contest.rate_limit` (`judge/models/contest.py:326`).
- **Contest ranking page:** already the right leaderboard for a Kaggle-style contest.

**Bottom line:** the *grading path* already works end-to-end (submit zip → judge runs checker → partial score via IOI format), and ranking/rate-limiting are already solved by hosting as a contest. The remaining wins are: (a) move the training dataset onto the problem itself, (b) eliminate hand-written checkers via a metric library, (c) fix the submit-page bottleneck for big files.

---

## 2. Target UX

### 2.1 Author flow
1. Create problem, tick **"Output-only / dataset problem"** in problem edit page (not buried in `problem_data`).
2. Upload **one** *public input dataset* file (zip/csv/parquet) — exposed to solvers via a "Download dataset" button on the problem page.
3. Upload **one** *private answer key* file + pick a **scoring metric** from a dropdown (accuracy, MSE, RMSE, MAE, F1, AUC, log-loss, custom-python). For custom, paste a small `score(submission_path, answer_path) -> float` snippet.
4. Set max submission file size (default 50 MB), max submissions per day (default 5, Kaggle-like), and whether the score is "higher is better".
5. Save. Done — no `init.yml` editing, no zip packing.

### 2.2 Solver flow
1. Open problem → see statement + **"Download input"** button + **"Submit prediction"** button.
2. Click submit → drag-and-drop a single file (csv/zip/json — whatever the problem expects). Big files (>10 MB) use **presigned direct upload** to S3 so the browser doesn't proxy through Django.
3. See submission row immediately with status `Queued`; on grade, see the metric value (e.g. `RMSE = 0.4123`).
4. Problem page shows **leaderboard** (best score per user) and "submissions remaining today: 3/5".

---

## 3. Design

### 3.1 Reuse vs. new

We **reuse** the existing pipeline end-to-end:
- `OUTPUT` language, `output_only` flag, `submission_source_file` URL passing, judge bridge, IOI partial scoring.
- `ProblemData.custom_checker` (FileField + `FileEditWidget`) — the inline-editable checker field is exactly where metric code belongs.
- Existing `direct_upload.py` for presigned S3 uploads.
- `RankedSubmissions` ("Best solutions") view as the per-problem leaderboard.
- Contest hosting (`ContestProblem.max_submissions`, `Contest.rate_limit`, contest ranking page) for cap + ranking.

We **add** a thin layer on top:
- A new general-purpose **`ProblemAttachment`** model — multiple downloadable files per problem (Codeforces-style "problem materials"). Not Kaggle-specific: equally useful for shipping `interactor.cpp` / `local_tester.sh` for interactive problems, sample images, reference PDFs, starter code, etc. Managed in a new "Attachments" tab on the problem edit page, separate from the problem-data (grader) tab.
- New entries in the existing `checker` choice list — `csv_accuracy`, `csv_rmse`, `csv_mae`, `csv_f1`, `csv_auc`, `csv_logloss`. **Implementations live in the judge-server fork** (alongside existing built-in checkers like `standard`/`floats`/`sorted`), not the site. The site only adds the choice keys to `CHECKERS` and exposes `checker_args` ({has_header, id_column, label_column}) in the form. No `ProblemDataCompiler` translation hack.
- A presigned-upload submit path for output-only submissions (UX win for large files, replaces the JSZip-in-browser proxy through Django).

No changes to `ProblemData`. No new judge-bridge protocol. No new submission storage. No GPU. No new leaderboard. Submission format goes in the statement; size cap stays global. Everything is the existing path with nicer ergonomics.

### 3.2 New model — `ProblemAttachment`

Add to `judge/models/problem.py` (or a new `judge/models/problem_attachment.py`):

```python
class ProblemAttachment(models.Model):
    problem = models.ForeignKey(
        Problem, related_name='attachments', on_delete=models.CASCADE,
    )
    file = models.FileField(
        upload_to=problem_attachment_path,   # 'problem_attachments/<code>/<filename>'
        storage=problem_data_storage,
    )
    description = models.CharField(max_length=255, blank=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'id']
```

One migration. Register in `judge/models/__init__.py`. No changes to `ProblemData`.

**Why a separate model + tab (not a field on `ProblemData`):**
- Multiple files per problem; `ProblemData` is one-to-one and has no clean place for an ordered list.
- Different audience: `ProblemData` is grader-facing (test cases, checker config); attachments are solver-facing (downloadable materials).
- Codeforces-style materials are reusable beyond Kaggle (sample images, starter code, reference PDFs).
- Permission story can diverge later (e.g., allow problem editors who aren't testers to manage attachments).

The **scoring metric** is still configured via the existing `ProblemData.checker` dropdown, now extended with CSV-aware metrics (see §3.3). The **submission format hint** lives in the problem statement. The **size cap** stays global.

The "is this a Kaggle-style problem?" signal is derived: `output_only=True AND attachments.exists()`.

### 3.3 New checker types — implemented in judge-server

Extend the `CHECKERS` choice list in `judge/models/problem_data.py:34-48` (site-side) with:

| Key | Metric | Direction |
|---|---|---|
| `csv_accuracy` | exact-match accuracy on label column | higher better |
| `csv_rmse` | root mean squared error on numeric column | lower better |
| `csv_mae` | mean absolute error on numeric column | lower better |
| `csv_f1` | macro F1 on label column | higher better |
| `csv_auc` | ROC AUC (binary) on probability column | higher better |
| `csv_logloss` | log loss on probability column | lower better |

**Implementation lives in the judge-server fork**, alongside the existing built-in checkers (`dmoj/checkers/standard.py`, `floats.py`, `sorted.py`, etc.). Each new checker:
1. Reads judge output (answer key) and submission output as CSV.
2. Joins on the `id_column`, computes the metric on the `label_column`.
3. Returns the standard `CheckerResult(success, points, feedback)` — `points` ∈ [0, 1] (lower-better metrics normalize internally), `feedback` carries the raw metric value (e.g. `"RMSE = 0.4123"`).

`checker_args` (existing JSON field, already supported) carries:
```json
{"has_header": true, "id_column": "id", "label_column": "target"}
```

Site-side: extend `checker_args_cleaner` (`judge/views/problem_data.py:73`) to render these as plain form inputs when a `csv_*` checker is selected, similar to how `floats` exposes a precision arg today. `ProblemDataCompiler.make_init()` writes them through to init.yml verbatim — no translation needed since the judge understands the new checker keys natively.

**Site/judge coordination:** the choice keys must be added in lockstep — site rejects unknown checkers at form-clean time. Adding more metrics later means a judge-server release plus a site-side enum addition. Acceptable for an infrequent change.

### 3.3b Metric semantics

Each checker returns `(score, feedback)` where `score ∈ [0, 1]` (1 = perfect). Lower-better metrics normalize internally. `init.yml` uses `partial: true` so `score` scales the test case's points — same machinery the rest of the site uses. No new ranking semantics, no `higher_is_better` field needed.

`feedback` carries the raw metric value (e.g. `"RMSE = 0.4123"`) so it shows up in the submission detail view. The "Best solutions" page ranks by `case_points`, which is the partial-scaled value.

### 3.4 Attachment management & distribution

**Edit page (new "Attachments" tab):**
- New URL `/problem/<code>/attachments/` (alongside existing `/problem/<code>/edit/` and `/problem/<code>/test_data/`).
- Permissions: same as problem-edit (problem authors + curators).
- UI: drag-drop multi-file upload zone, table of existing attachments with inline-editable description, drag-handle reorder, delete button. AJAX endpoints for upload / reorder / delete (mirror the patterns used in `judge/views/quiz.py:1952` for essay attachments — already proven in this codebase).
- Big files use the existing `direct_upload.py` presigned-upload flow.

**Problem page:**
- New "Files" / "Attachments" section listing each attachment as `<description> — <filename> (size)` with a download link. Hidden if no attachments.

**Download view:**
- `attachment_download(problem_code, attachment_id)`. For S3, 302 to a short-lived presigned GET URL with `Content-Disposition: attachment`. For local storage, stream via `FileResponse`. Auth: same as problem visibility (respects contest visibility).

**Answer key:** unchanged — lives in `ProblemData.zipfile`, only read by the judge worker.

### 3.5 Submit UX upgrade

- Update `templates/problem/submit.html` (existing output-only branch) to render a clean drag-drop zone. No new template needed.
- Files use `direct_upload.py` flow: browser POSTs directly to S3 with a presigned URL, then submits to Django with just the resulting key. The submit view receives `{key}`, builds the same `submission_source_file` URL the judge expects, and creates a `Submission` with `language=OUTPUT`. Bypasses the multipart-through-Django bottleneck.
- For local storage backend, fall back to a normal multipart POST.
- Size cap remains the global limit (`judge/forms.py:198`), bumped to a sensible value (e.g. 50 MB) site-wide if needed.

**Spam concern:** presigned URLs are gated by the existing token system (`judge/views/direct_upload.py:29-76`) — the server first issues a short-lived `upload_token` scoped to `(profile_id, upload_to, max_size, prefix)`, and only then can the client request a presigned URL. Same trust model as today's pagedown image upload. Add a simple `django-ratelimit` decorator on token issuance (e.g., 30/min/user) as belt-and-suspenders, and rely on the existing per-problem submission rate limit (`Contest.rate_limit`, plus the global submission limiter) to bound the actual judging cost.

### 3.6 Submission cap & leaderboard — covered by existing features

- **Cap:** host the Kaggle problem inside a contest and set `ContestProblem.max_submissions`. `Contest.rate_limit` provides anti-spam. No new code.
- **Leaderboard outside contests:** `RankedSubmissions` ("Best solutions for X") view orders submissions by `case_points` desc — exactly the per-user best-score ranking Kaggle shows.
- **Leaderboard inside contests:** standard contest ranking page.
- *Optional polish:* add a "Leaderboard" link on the problem page that points to the existing `RankedSubmissions` URL when `dataset_config` exists, since users won't otherwise know the page exists.

### 3.7 What we explicitly are NOT building (YAGNI)

- No private-test-set / public-leaderboard split (Kaggle's "Public LB / Private LB"). Single hidden answer; single score. Easy to add later.
- No notebook hosting, no on-platform training, no GPU.
- No team-submission pooling.
- No new leaderboard / ranking / submission-cap views — the existing contest + `RankedSubmissions` machinery covers it.
- No automated metric verification beyond "the checker ran and returned a number."
- No re-grading on rubric change (authors who change the answer file just create a new problem or accept that scores shift).

---

## 4. Build order

Each step is independently shippable:

**Site repo:**
1. **`ProblemAttachment` model + Attachments tab**: migration, model, form, AJAX upload/reorder/delete endpoints, edit-page template. Mirrors the quiz essay-attachment patterns already in the codebase. *Useful immediately for any problem type — interactive runners, sample images, starter code, datasets.*
2. **Attachment download view** + "Files" section on the problem page (302 to presigned GET on S3, stream on local).
3. **CSV checker keys**: add `csv_*` entries to `CHECKERS` choice list + form support for `checker_args` ({has_header, id_column, label_column}). Gated on the judge-server release.
4. **Submit page upgrade**: branch in `submit.html` with drag-drop + presigned direct upload to S3 via the existing `direct_upload.py` flow; bypasses Django proxy. Add `django-ratelimit` on token issuance.
5. **Polish:** "Leaderboard" link on the problem page pointing to `RankedSubmissions`, dark-mode pass on new UI, Playwright smoke test.

**Judge-server fork (parallel track, can ship independently of site step 3):**
- Implement the six CSV checkers under `dmoj/checkers/csv_*.py` mirroring the conventions of existing built-in checkers. Unit tests per metric on synthetic data.
- Release a tagged judge-server build; deploy to judge nodes; then merge site step 3.

Steps 1–2 are immediately useful for *any* problem type and don't depend on the checker work. Steps 3–4 are the Kaggle-specific completion. Step 5 is polish.

Steps 1–4 are the MVP; 5 is quality-of-life.

---

## 5. Open questions

- **Auth on dataset download:** gate on "can view problem" only, or require login? Recommend: same as problem visibility (anonymous if problem is public, login if not). Matches today's behavior for problem statements.
- **Re-scoring:** if author edits the answer file, do we rejudge all existing submissions? Recommend: no automatic rejudge; offer a manual "rejudge all" button (already exists for normal problems) and document the gotcha.
- **Score display:** show the raw metric (`0.8423 RMSE`) or the points (`74.23 / 100`)? Recommend: both — points in the submissions list (consistent with rest of site), raw metric on the leaderboard and submission detail page.
- **Contest integration:** does a dataset problem in a contest use the contest's format scoring as-is? Recommend: yes — partial points flow through IOI format naturally.

---

## 6. Risk / impact

- **Migration safety:** adding a new model is additive; no changes to existing `Problem` / `ProblemData` schema.
- **Storage growth:** dataset files can be large. Recommend a hard cap (e.g. 1 GB per input file) enforced at upload, and a periodic cleanup task for orphaned `dataset_problems/` files.
- **Judge load:** custom-Python checkers run inside the existing sandbox; large outputs (e.g. 100 MB CSVs) could blow checker memory. Recommend documenting a per-metric memory budget and using streaming reads (`pandas.read_csv` with `chunksize` or numpy memmap) in the built-in metrics.
- **Existing output-only problems:** continue to work unchanged. The new flow is opt-in via `DatasetProblem`.
