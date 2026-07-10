"""quiz_leak_check: any signal that a contest quiz was visible to non-trusted users.

Sibling of `submission_leak_check` (problems) — a contest quiz is a distinct
surface from a problem, so it gets its own check row and verdict rather than
being folded into the problem-leak check.

"Trusted" is the SAME contest-scoped set as the problem-leak check (authors +
curators + testers of THIS contest, plus superusers) — reused via
`_trusted_profile_ids` so the two checks can't drift apart.

Two leak signals (mirroring the two problem signals):

  1. **Quiz-attempt leak** — a non-trusted user has a QuizAttempt on a contest
     quiz. They've actually taken it; the quiz is "burned" for scoring
     integrity. Analogous to the problem submission leak.

  2. **Role leak** — a non-trusted user holds an author/curator/tester role on
     a contest quiz. They have access to the quiz source even if they haven't
     attempted it. Analogous to the problem role leak.

Either signal flags FAIL. Both are listed in details_json (quiz_leakers /
quiz_role_leakers) so the organizer sees who, how (attempt/role), and on which
quiz.

This check only cares about who could have SEEN a quiz — NOT its content or
quality. Quiz content review is a separate, deferred feature.

DB-only check — no LLM call.
"""

from django.db.models import Count, Max, Prefetch
from django.utils.translation import gettext as _, gettext_lazy

from judge.models import Profile
from judge.models.contest_review import ContestReviewCheckResult
from judge.models.quiz import Quiz, QuizAttempt
from judge.review.base import CheckResultData
from judge.review.contest_base import ContestReviewCheck
from judge.review.contest_checks.submission_leak_check import (
    _DETAILS_CAP,
    _trusted_profile_ids,
)


def _collect_quiz_role_leaks(quizzes, trusted_ids):
    """Quiz analogue of `_collect_role_leaks` in submission_leak_check.

    `quizzes` is a Quiz QuerySet (the contest's quiz slots). Returns a list of
    {user_id, username, quiz_code, quiz_title, roles} for each non-trusted,
    non-admin user holding an author/curator/tester role on any of them.

    Prefetches the three role M2Ms (each with select_related("user") baked in)
    so the whole collection is a CONSTANT 4 queries regardless of quiz count,
    matching `_collect_role_leaks`. As there, the loop must use plain `.all()` —
    a `.select_related(...)` there would bypass the prefetch cache and re-query
    per quiz.
    """
    rows = []
    quizzes = quizzes.prefetch_related(
        Prefetch("authors", queryset=Profile.objects.select_related("user")),
        Prefetch("curators", queryset=Profile.objects.select_related("user")),
        Prefetch("testers", queryset=Profile.objects.select_related("user")),
    )
    for q in quizzes:
        per_user_roles = {}
        for role, qs in (
            ("author", q.authors.all()),
            ("curator", q.curators.all()),
            ("tester", q.testers.all()),
        ):
            for profile in qs:
                if profile.id in trusted_ids:
                    continue
                if profile.user.is_superuser:
                    continue
                key = (profile.id, profile.user.username)
                per_user_roles.setdefault(key, set()).add(role)

        for (uid, username), roles in per_user_roles.items():
            rows.append(
                {
                    "user_id": uid,
                    "username": username,
                    "quiz_code": q.code,
                    "quiz_title": q.title,
                    "roles": sorted(roles),
                }
            )
    return rows


class QuizLeakCheck(ContestReviewCheck):
    id = "quiz_leak_check"
    display_name = gettext_lazy("Quiz leak")

    def run(self, contest, run):
        # Quiz slots: ContestProblem rows with a quiz (problem is null).
        quiz_ids = list(
            contest.contest_problems.filter(quiz__isnull=False).values_list(
                "quiz_id", flat=True
            )
        )
        if not quiz_ids:
            return CheckResultData(
                status=ContestReviewCheckResult.SKIPPED,
                reason=_("Contest has no quizzes."),
            )

        trusted_ids = _trusted_profile_ids(contest)

        # Signal 1: quiz attempts by non-trusted, non-admin users.
        attempt_leakers = (
            QuizAttempt.objects.filter(quiz_id__in=quiz_ids)
            .exclude(user_id__in=trusted_ids)
            .exclude(user__user__is_superuser=True)
            .values(
                "user_id",
                "user__user__username",
                "quiz__code",
                "quiz__title",
            )
            .annotate(
                attempt_count=Count("id"),
                latest_attempt_id=Max("id"),
            )
            .order_by("-attempt_count", "user_id")
        )
        attempt_total = attempt_leakers.count()
        attempt_capped = list(attempt_leakers[:_DETAILS_CAP])
        for row in attempt_capped:
            row["username"] = row.pop("user__user__username")
            row["quiz_code"] = row.pop("quiz__code")
            row["quiz_title"] = row.pop("quiz__title")

        # Signal 2: role holders on contest quizzes who aren't on the team.
        role_rows = _collect_quiz_role_leaks(
            Quiz.objects.filter(id__in=quiz_ids), trusted_ids
        )
        role_total = len(role_rows)
        role_capped = role_rows[:_DETAILS_CAP]

        details = {
            "quiz_leakers": attempt_capped,
            "quiz_leakers_total": attempt_total,
            "quiz_leakers_shown": len(attempt_capped),
            "quiz_role_leakers": role_capped,
            "quiz_role_leakers_total": role_total,
            "quiz_role_leakers_shown": len(role_capped),
            "trusted_user_count": len(trusted_ids),
        }

        if attempt_total == 0 and role_total == 0:
            return CheckResultData(
                status=ContestReviewCheckResult.SUCCESS,
                reason=_("No leak signals found."),
                details=details,
            )

        parts = []
        if attempt_total:
            parts.append(_("%(n)d quiz-attempt leak(s)") % {"n": attempt_total})
        if role_total:
            parts.append(_("%(n)d quiz role-access leak(s)") % {"n": role_total})
        reason = _("Quiz leak detected: %(detail)s.") % {
            "detail": ", ".join(str(p) for p in parts),
        }

        return CheckResultData(
            status=ContestReviewCheckResult.FAIL,
            reason=reason,
            details=details,
        )
