"""Contest confidentiality checks, separate from technical AI review.

Standalone problem publication does not require secrecy. Contest publication
checks access in the context of the contest's current preparation team instead.
"""

from django.urls import reverse
from django.utils.translation import gettext as _, ngettext

from judge.models import Contest, Problem, Profile, Quiz
from judge.review.snapshots import _contest_exposure


def contest_publication_exposure(contest, *, detail_limit=0):
    """Live contest access evidence, without building an entire AI package."""
    items = list(contest.contest_problems.values("problem_id", "quiz_id"))
    roles = {
        "authors": list(contest.authors.values_list("pk", flat=True)),
        "curators": list(contest.curators.values_list("pk", flat=True)),
        "testers": list(contest.testers.values_list("pk", flat=True)),
    }
    return _contest_exposure(contest, items, roles, detail_limit=detail_limit)


def publication_exposure_concern(obj):
    if isinstance(obj, Contest):
        return contest_publication_exposure(obj)["material_concern"]
    return False


def publication_access_details(obj, *, limit=5):
    """Bounded, batch-loaded evidence for an already-authorized review reader.

    Never include source code or names of unrelated private contests. The caller
    must authorize the contest review before requesting account-level evidence.
    """
    if not isinstance(obj, Contest):
        return None
    evidence = contest_publication_exposure(obj, detail_limit=limit)
    if not evidence["material_concern"]:
        return {"material_concern": False}
    submissions = evidence["submission_details"]
    attempts = evidence["quiz_attempt_details"]
    capabilities = evidence["access_capabilities"]
    activity_groups = evidence["submission_groups"] + evidence["quiz_attempt_groups"]
    profile_ids = {row["user_id"] for row in activity_groups} | {
        row["profile_id"] for row in capabilities
    }
    problem_ids = {row["problem_id"] for row in evidence["submission_groups"]} | {
        row["item_id"] for row in capabilities if row["kind"] == "problem"
    }
    quiz_ids = {row["quiz_id"] for row in evidence["quiz_attempt_groups"]} | {
        row["item_id"] for row in capabilities if row["kind"] == "quiz"
    }
    profiles = {
        profile.pk: profile for profile in Profile.get_cached_instances(*profile_ids)
    }
    problems = {
        problem.pk: problem for problem in Problem.get_cached_instances(*problem_ids)
    }
    quizzes = {quiz.pk: quiz for quiz in Quiz.objects.filter(pk__in=quiz_ids)}
    groups = {}

    def group_for(kind, item_id, profile_id):
        key = (kind, item_id, profile_id)
        if key not in groups:
            item = problems[item_id] if kind == "problem" else quizzes[item_id]
            groups[key] = {
                "kind": kind,
                "item_id": item_id,
                "profile": profiles[profile_id],
                "item_name": item.name if kind == "problem" else item.title,
                "item_url": reverse(
                    "problem_detail" if kind == "problem" else "quiz_detail",
                    args=[item.code],
                ),
                "submission_count": 0,
                "attempt_count": 0,
                "roles": set(),
                "records": [],
                "last_date": None,
            }
        return groups[key]

    for row in evidence["submission_groups"]:
        group = group_for("problem", row["problem_id"], row["user_id"])
        group["submission_count"] = row["count"]
        group["last_date"] = row["last_date"]
    for row in evidence["quiz_attempt_groups"]:
        group = group_for("quiz", row["quiz_id"], row["user_id"])
        group["attempt_count"] = row["count"]
        group["last_date"] = row["last_date"]
    for row in submissions:
        group_for("problem", row["problem_id"], row["user_id"])["records"].append(
            {
                "activity": _("Submission #%(id)s") % {"id": row["id"]},
                "activity_url": reverse("submission_status", args=[row["id"]]),
                "date": row["date"],
                "context": (
                    _("In this contest")
                    if row["contest_object_id"] == obj.pk
                    else _("Outside this contest")
                ),
            }
        )
    for row in attempts:
        quiz = quizzes[row["quiz_id"]]
        group_for("quiz", row["quiz_id"], row["user_id"])["records"].append(
            {
                "activity": _("Quiz attempt #%(id)s") % {"id": row["id"]},
                "activity_url": reverse(
                    "quiz_attempt_status", args=[quiz.code, row["id"]]
                ),
                "date": row["start_time"],
                "context": "",
            }
        )
    role_labels = {
        "problem": {
            "authors": _("Problem author"),
            "curators": _("Problem curator"),
            "testers": _("Problem tester"),
        },
        "quiz": {
            "authors": _("Quiz author"),
            "curators": _("Quiz curator"),
            "testers": _("Quiz tester"),
        },
    }
    for row in capabilities:
        group_for(row["kind"], row["item_id"], row["profile_id"])["roles"].add(
            row["role"]
        )
    for group in groups.values():
        count = group["submission_count"]
        group["submission_label"] = ngettext(
            "%(count)s submission", "%(count)s submissions", count
        ) % {"count": count}
        count = group["attempt_count"]
        group["attempt_label"] = ngettext(
            "%(count)s quiz attempt", "%(count)s quiz attempts", count
        ) % {"count": count}
        group["record_count"] = group["submission_count"] + group["attempt_count"]
        group["access_labels"] = [
            label
            for role, label in role_labels[group["kind"]].items()
            if role in group["roles"]
        ]
        group["shown_records"] = len(group["records"])
    ordered = sorted(
        groups.values(),
        key=lambda group: (
            group["last_date"] is None,
            -group["last_date"].timestamp() if group["last_date"] else 0,
            group["profile"].pk,
            group["kind"],
            group["item_id"],
        ),
    )
    submission_count = sum(row["count"] for row in evidence["submissions"])
    attempt_count = sum(row["count"] for row in evidence["quiz_attempts"])
    summary = [
        ngettext("%(count)s account", "%(count)s accounts", len(profile_ids))
        % {"count": len(profile_ids)}
    ]
    if problem_ids:
        summary.append(
            ngettext("%(count)s problem", "%(count)s problems", len(problem_ids))
            % {"count": len(problem_ids)}
        )
    if quiz_ids:
        summary.append(
            ngettext("%(count)s quiz", "%(count)s quizzes", len(quiz_ids))
            % {"count": len(quiz_ids)}
        )
    if submission_count:
        summary.append(
            ngettext("%(count)s submission", "%(count)s submissions", submission_count)
            % {"count": submission_count}
        )
    if attempt_count:
        summary.append(
            ngettext("%(count)s quiz attempt", "%(count)s quiz attempts", attempt_count)
            % {"count": attempt_count}
        )
    return {
        "material_concern": True,
        "groups": ordered,
        "visible_groups": ordered[:5],
        "more_groups": ordered[5:],
        "summary": summary,
        "submission_count": submission_count,
        "attempt_count": attempt_count,
        "has_access_roles": bool(capabilities),
    }
