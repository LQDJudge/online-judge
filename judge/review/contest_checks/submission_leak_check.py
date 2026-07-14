"""submission_leak_check: any signal that a contest problem was visible to non-trusted users.

"Trusted" means anyone who legitimately had access to prep THIS contest:
  - Contest authors / curators / testers
  - Superusers (admins reviewing the contest itself)

Notably we do NOT trust per-problem authors/curators/testers. Trust is bound
to the contest context, not the problem artifact. Reasoning: a problem reused
across contests carries its old curator/tester list forever, so trusting them
would silently whitelist anyone who ever touched the problem — even if their
involvement was unrelated to the new contest. A contest organizer who wants
a per-problem author on the prep team needs to add them to the contest's
curators/testers explicitly.

Two leak signals reported in one check (cheap to compute together):

  1. **Submission leak** — a non-trusted user has Submissions on a contest
     problem. They've actually solved or attempted it; the problem is
     "burned" for the new contest's scoring integrity.

  2. **Role leak** — a non-trusted user holds an author/curator/tester role
     on a contest problem. They have access to the problem source even if
     they haven't submitted yet. Catches the case where a problem reused
     across contests retains its old author whose involvement was unrelated
     to the new contest.

Either signal flags FAIL. Both are listed in details_json so the organizer
sees who, how (sub/role), and on which problem.

Quizzes are handled by the sibling `quiz_leak_check` — a contest quiz is a
distinct surface, so it gets its own check row/verdict rather than being
folded in here.

DB-only check — no LLM call, fast even on contests with many problems.
"""

from django.db.models import Count, Max, Prefetch
from django.utils.translation import gettext as _, gettext_lazy

from judge.models import Profile, Submission
from judge.models.contest_review import ContestReviewCheckResult
from judge.review.base import CheckResultData
from judge.review.contest_base import ContestReviewCheck

try:
    from django.conf import settings

    _DETAILS_CAP = getattr(settings, "AUTO_REVIEW_CONTEST_LEAK_DETAILS_CAP", 50)
except Exception:
    _DETAILS_CAP = 50


def _trusted_profile_ids(contest):
    """Contest-scoped trust only — authors + curators + testers of THIS contest.

    Per-problem trust deliberately excluded; see module docstring for why.
    Superusers are filtered separately in the query (via `user__is_superuser=True`)
    rather than enumerated here, so we don't have to round-trip all admin
    Profile ids into Python and back.

    Shared with `quiz_leak_check` (imported there) so both leak checks agree on
    exactly who counts as trusted for THIS contest.
    """
    trusted = set()
    trusted.update(contest.authors.values_list("id", flat=True))
    trusted.update(contest.curators.values_list("id", flat=True))
    trusted.update(contest.testers.values_list("id", flat=True))
    return trusted


def _collect_role_leaks(contest, trusted_ids):
    """List (username, problem_code, problem_name, roles) where roles is a
    sorted list of strings like ['author', 'tester'] for each non-trusted,
    non-admin user holding any role on a contest problem.

    Admins are excluded for symmetry with the submission-half of the check —
    superusers are implicitly trusted across the whole pipeline (their
    submissions are filtered out via user__user__is_superuser=True). Listing
    them as role leakers here would falsely flag an admin who's the original
    author of a reused problem.

    Prefetch the three role M2Ms (each with select_related("user") baked into
    the Prefetch queryset, so profile.user.username costs no extra query) — this
    is a CONSTANT 4 queries (1 for problems + 3 prefetch) regardless of how many
    problems the contest has, instead of O(3 × problems). NOTE: the loop below
    must use plain `.all()` on each relation — appending `.select_related(...)`
    there would build a new queryset that ignores the prefetch cache and re-hits
    the DB per problem, defeating the point.
    """
    rows = []
    problems = contest.problems.prefetch_related(
        Prefetch("authors", queryset=Profile.objects.select_related("user")),
        Prefetch("curators", queryset=Profile.objects.select_related("user")),
        Prefetch("testers", queryset=Profile.objects.select_related("user")),
    )
    for p in problems:
        per_user_roles = {}
        for role, qs in (
            ("author", p.authors.all()),
            ("curator", p.curators.all()),
            ("tester", p.testers.all()),
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
                    "problem_code": p.code,
                    "problem_name": p.name,
                    "roles": sorted(roles),
                }
            )
    return rows


class SubmissionLeakCheck(ContestReviewCheck):
    id = "submission_leak_check"
    display_name = gettext_lazy("Problem leak")

    def run(self, contest, run):
        problem_ids = list(contest.problems.values_list("id", flat=True))
        if not problem_ids:
            return CheckResultData(
                status=ContestReviewCheckResult.SKIPPED,
                reason=_("Contest has no problems."),
            )

        trusted_ids = _trusted_profile_ids(contest)

        # Signal 1: submissions by non-trusted, non-admin users.
        leakers = (
            Submission.objects.filter(problem_id__in=problem_ids)
            .exclude(user_id__in=trusted_ids)
            .exclude(user__user__is_superuser=True)
            .values(
                "user_id",
                "user__user__username",
                "problem__code",
                "problem__name",
            )
            .annotate(
                submission_count=Count("id"),
                latest_submission_id=Max("id"),
            )
            .order_by("-submission_count", "user_id")
        )
        sub_total = leakers.count()
        sub_capped = list(leakers[:_DETAILS_CAP])
        for row in sub_capped:
            row["username"] = row.pop("user__user__username")
            row["problem_code"] = row.pop("problem__code")
            row["problem_name"] = row.pop("problem__name")

        # Signal 2: role holders on contest problems who aren't on the
        # contest team. They have problem source access.
        role_rows = _collect_role_leaks(contest, trusted_ids)
        role_total = len(role_rows)
        role_capped = role_rows[:_DETAILS_CAP]

        if sub_total == 0 and role_total == 0:
            return CheckResultData(
                status=ContestReviewCheckResult.SUCCESS,
                reason=_("No leak signals found."),
                details={
                    "leakers": [],
                    "role_leakers": [],
                    "trusted_user_count": len(trusted_ids),
                },
            )

        parts = []
        if sub_total:
            parts.append(_("%(n)d submission leak(s)") % {"n": sub_total})
        if role_total:
            parts.append(_("%(n)d role-access leak(s)") % {"n": role_total})
        reason = _("Problem leak detected: %(detail)s.") % {
            "detail": ", ".join(str(p) for p in parts),
        }

        return CheckResultData(
            status=ContestReviewCheckResult.FAIL,
            reason=reason,
            details={
                "leakers": sub_capped,
                "leakers_total": sub_total,
                "leakers_shown": len(sub_capped),
                "role_leakers": role_capped,
                "role_leakers_total": role_total,
                "role_leakers_shown": len(role_capped),
                "trusted_user_count": len(trusted_ids),
            },
        )
