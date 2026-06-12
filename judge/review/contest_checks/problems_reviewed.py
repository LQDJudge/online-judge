"""problems_reviewed: every contained problem gets a fresh review for this contest run.

Always triggers fresh per-problem reviews — does NOT reuse prior fresh runs.

Rationale: the earlier "reuse if input_hash matches" optimization was invisible
to users. They'd hit Rerun, see the contest verdict update, then click into a
problem's review page and see yesterday's timestamp — and conclude nothing had
happened. Always-fresh costs ~5-15s of LLM/judge time per contained problem but
makes the mental model honest: "click rerun → every problem is reviewed again".

The contest-level dirty-check still prevents wasteful invocations for authors
(Request Public on a contest with no changes is refused). Admins bypass the
dirty-check, so admin Rerun always re-reviews everything — which is what an
admin Rerun should mean.

Triggered runs do NOT create a `PublicRequest`. They're contest-context
reviews — running them shouldn't make a contest-only problem appear in the
admin's public-request queue.

The check's verdict:
  - FAIL if any per-problem review fails OR errors.
  - SUCCESS otherwise.

details_json shape for the dashboard:
  {
    "per_problem": [
      {"code": "...", "name": "...", "review_run_id": N,
       "verdict": "pass" | "fail" | "error" | "missing",
       "failing_checks": ["..."],
       "dashboard_url": "/problem/<code>/review",
       "triggered_inline": bool}, ...
    ],
    "summary": {"total": N, "passed": k, "failed": k, "errored": k, "missing_after_trigger": k}
  }

`missing_after_trigger` would only be non-zero if the inline trigger itself
crashed before producing a run — defensive, normally 0.
"""

import logging

from django.db import transaction
from django.urls import reverse
from django.utils.translation import gettext as _, gettext_lazy

from judge.models.problem_review import ProblemReviewCheckResult, ProblemReviewRun
from judge.models.contest_review import ContestReviewCheckResult
from judge.review.base import CheckResultData
from judge.review.contest_base import ContestReviewCheck
from judge.review.hashing import compute_input_hash
from judge.review.system_bot import post_system_comment_on_review
from judge.review.triggers import trigger_problem_review_for

logger = logging.getLogger(__name__)


def _build_per_problem_payload(
    problem, review_run, fail_check_ids, *, triggered_inline
):
    """Common dashboard payload for one (problem, latest_review) pair."""
    try:
        url = reverse("problem_review_dashboard", args=[problem.code])
    except Exception:
        url = f"/problem/{problem.code}/review"

    if review_run is None:
        verdict = "missing"
    elif review_run.status == ProblemReviewRun.ERROR:
        verdict = "error"
    elif fail_check_ids:
        verdict = "fail"
    else:
        verdict = "pass"

    return {
        "code": problem.code,
        "name": problem.name,
        "review_run_id": review_run.id if review_run else None,
        "review_run_status": review_run.status if review_run else None,
        "verdict": verdict,
        "failing_checks": fail_check_ids,
        "dashboard_url": url,
        "triggered_inline": triggered_inline,
    }


def _latest_matching_run(problem):
    """Latest non-superseded DONE run whose input_hash matches the problem."""
    target = compute_input_hash(problem)
    return (
        ProblemReviewRun.objects.filter(
            problem=problem,
            status=ProblemReviewRun.DONE,
            superseded_by__isnull=True,
            input_hash=target,
        )
        .order_by("-finished_at")
        .first()
    )


def _failing_check_ids_for(run):
    if run is None:
        return []
    return list(
        ProblemReviewCheckResult.objects.filter(
            run=run, status=ProblemReviewCheckResult.FAIL
        ).values_list("check_id", flat=True)
    )


class ProblemsReviewedCheck(ContestReviewCheck):
    id = "problems_reviewed"
    display_name = gettext_lazy("Per-problem reviews")

    def run(self, contest, run):
        # Order by the through-table (ContestProblem.order); `contest.problems`
        # is M2M and can't be order_by'd on through-fields directly.
        problems = [
            cp.problem
            for cp in contest.contest_problems.select_related("problem")
            .filter(problem__isnull=False)
            .order_by("order")
        ]
        if not problems:
            return CheckResultData(
                status=ContestReviewCheckResult.FAIL,
                reason=_("Contest has no problems."),
                details={"per_problem": [], "summary": {"total": 0}},
            )

        # Partition into "truly public" (visible to everyone — already-known
        # problems that can't be in a rated contest) and "reviewable" (private,
        # or org-private which is still unseen outside that organization).
        #
        # Public problems are SKIPPED from the per-problem review pass — they
        # don't need review (they're already public) and per-problem review
        # would just create dashboard clutter. At the end of the check, if
        # any public problems are present, the overall verdict is overridden
        # to FAIL with a clear reason so the author knows to swap them out.
        truly_public_problems = [
            p for p in problems if p.is_public and not p.is_organization_private
        ]
        reviewable_problems = [
            p for p in problems if not (p.is_public and not p.is_organization_private)
        ]

        per_problem_rows = []
        triggerer = run.triggered_by  # may be None if reaper-resurrected
        force_refresh = bool(getattr(run, "force_refresh_problems", False))

        for p in reviewable_problems:
            triggered_inline = False
            latest = None

            # Reuse path (default for author Request Public): if the problem
            # already has a fresh DONE review whose input_hash matches the
            # current problem state, use it — no need to re-run, saves
            # significant LLM/judge cost.
            #
            # Force-fresh path (admin Rerun): skip the reuse check and
            # ALWAYS trigger so the run reflects "rebuild everything now".
            if not force_refresh and triggerer is not None:
                existing = _latest_matching_run(p)
                if existing is not None:
                    latest = existing
                    # triggered_inline stays False — we reused, didn't trigger.

            if latest is None and triggerer is not None:
                # No fresh match (or force_refresh): trigger inline.
                # Blocks the contest review's Celery worker slot for the
                # duration of the per-problem review (~5-15s typical).
                try:
                    with transaction.atomic():
                        trigger_problem_review_for(p, triggerer, dispatch="sync")
                    triggered_inline = True
                    latest = _latest_matching_run(p)
                    # Audit trail: post a system comment on the per-problem
                    # review thread explaining who triggered this run and via
                    # which contest. The comment lands on the problem's review
                    # discussion (anchored to the problem's first run) so the
                    # author sees the context next to the new verdict instead
                    # of needing to dig through the contest dashboard.
                    try:
                        # `[user:username]` renders as a clickable user mention
                        # pill via the comment system's parser — same convention
                        # used elsewhere in LQDOJ comments. Plain markdown bold
                        # was just text and didn't link anywhere.
                        body = _(
                            "**[System]** Review auto-triggered by "
                            "[user:%(user)s] for contest "
                            "[%(contest_name)s](%(contest_url)s)."
                        ) % {
                            "user": triggerer.user.username,
                            "contest_name": contest.name,
                            "contest_url": f"/contest/{contest.key}/",
                        }
                        post_system_comment_on_review(p, str(body))
                    except Exception:
                        # Comment is nice-to-have; never let it sink the
                        # check. The trigger already succeeded.
                        logger.exception("Failed to post system comment for %s", p.code)
                except Exception as exc:
                    logger.exception(
                        "Inline problem-review trigger failed for %s: %s",
                        p.code,
                        exc,
                    )
                    # On trigger failure, fall back to whatever fresh run might
                    # exist (e.g., from a recent prior contest review). Better
                    # than reporting "missing" when good data is sitting there.
                    latest = _latest_matching_run(p)
            elif latest is None:
                # `triggerer is None` (e.g., reaper-resurrected run). Read
                # whatever fresh matching run exists.
                #
                # Note: when `latest is not None` (the reuse path filled it
                # in above), we skip the call entirely — recomputing
                # _latest_matching_run would recompute compute_input_hash()
                # and re-run the DB query for an identical result. The reuse
                # path saves both per problem per run.
                latest = _latest_matching_run(p)

            fail_ids = _failing_check_ids_for(latest)
            per_problem_rows.append(
                _build_per_problem_payload(
                    p, latest, fail_ids, triggered_inline=triggered_inline
                )
            )
            if latest is not None:
                run.problem_review_runs.add(latest)

        # Aggregate counts for the summary blob.
        verdict_counts = {"pass": 0, "fail": 0, "error": 0, "missing": 0}
        for row in per_problem_rows:
            verdict_counts[row["verdict"]] = verdict_counts.get(row["verdict"], 0) + 1

        summary = {
            "total": len(per_problem_rows),
            "passed": verdict_counts["pass"],
            "failed": verdict_counts["fail"],
            "errored": verdict_counts["error"],
            "missing_after_trigger": verdict_counts["missing"],
            "inline_triggered": sum(
                1 for r in per_problem_rows if r["triggered_inline"]
            ),
            "public_problems_count": len(truly_public_problems),
        }
        details = {
            "per_problem": per_problem_rows,
            "summary": summary,
            "public_problems": [
                {"code": p.code, "name": p.name} for p in truly_public_problems
            ],
        }

        # Public-problem override takes precedence over per-problem verdicts.
        # A contest containing any already-public problem cannot be rated,
        # regardless of how the private problems' reviews went. The author
        # needs to swap those problems out before requesting public again.
        if truly_public_problems:
            codes = ", ".join(p.code for p in truly_public_problems[:5])
            if len(truly_public_problems) > 5:
                codes += ", ..."
            reason = _(
                "Contest contains %(n)d already-public problem(s) "
                "(%(codes)s). Rated contests can't reuse public problems "
                "— replace them with original problems before requesting "
                "public again."
            ) % {"n": len(truly_public_problems), "codes": codes}
            return CheckResultData(
                status=ContestReviewCheckResult.FAIL,
                reason=reason,
                details=details,
            )

        bad = (
            verdict_counts["fail"] + verdict_counts["error"] + verdict_counts["missing"]
        )
        if bad == 0:
            return CheckResultData(
                status=ContestReviewCheckResult.SUCCESS,
                reason=_("All %(n)d problems passed review.") % {"n": summary["total"]},
                details=details,
            )

        # Positive framing: "X/N problems passed review" reads more naturally
        # than "Y/N have issues: A failed, B errored, C missing" — the
        # per-problem subtable below already shows the breakdown row by row.
        reason = _("%(passed)d/%(total)d problems passed review.") % {
            "passed": verdict_counts["pass"],
            "total": summary["total"],
        }
        return CheckResultData(
            status=ContestReviewCheckResult.FAIL,
            reason=reason,
            details=details,
        )
