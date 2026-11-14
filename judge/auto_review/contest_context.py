"""Collect current contest data and publication access."""

from django.db.models import Count, F, Max, Window
from django.db.models.functions import RowNumber
from django.utils import timezone

from judge.auto_review.context_utils import (
    _json_value,
    _scalars,
    digest_json,
)
from judge.auto_review.review_hash import contest_review_content
from judge.models import (
    Problem,
    Submission,
)
from judge.models.quiz import Quiz, QuizAttempt, QuizQuestion, QuizQuestionAssignment


def build_contest_context(contest, children, *, identity_only=False):
    # Root binds selected/attached child runs before calling this function.
    config = _scalars(contest, ("id", "access_code", "is_locked"))
    config["has_access_code"] = bool(contest.access_code)
    items = list(contest.contest_problems.order_by("order", "id").values())
    roles = (
        {}
        if identity_only
        else {
            name: sorted(getattr(contest, name).values_list("pk", flat=True))
            for name in ("authors", "curators", "testers", "organizations")
        }
    )
    quiz_ids = [item["quiz_id"] for item in items if item.get("quiz_id")]
    quizzes = list(Quiz.objects.filter(pk__in=quiz_ids).order_by("id"))
    assignments = list(
        QuizQuestionAssignment.objects.filter(quiz_id__in=quiz_ids)
        .order_by("quiz_id", "order", "id")
        .values()
    )
    question_ids = [assignment["question_id"] for assignment in assignments]
    quiz_content = {
        "quizzes": [
            _scalars(quiz, ("is_locked", "created_at", "is_public")) for quiz in quizzes
        ],
        "assignments": assignments,
        "questions": list(
            QuizQuestion.objects.filter(pk__in=question_ids)
            .order_by("id")
            .values(
                "id",
                "question_type",
                "title",
                "content",
                "choices",
                "correct_answers",
                "explanation",
                "shuffle_choices",
                "grading_strategy",
                "multiple_true_false_score_table",
            )
        ),
    }
    exposure = {}  # Live publication-only checks are never AI context.
    snapshot = {
        "kind": "contest",
        "contest_id": contest.pk,
        "config": config,
        "items": items,
        "children": children,
        "roles": roles,
        "quiz_content": quiz_content,
        "exposure": exposure,
    }
    snapshot["content_hash"] = digest_json(contest_review_content(snapshot))
    snapshot["input_hash"] = snapshot["content_hash"]
    return snapshot


def _contest_exposure(contest, items, roles, *, detail_limit=0):
    trusted = set(roles["authors"] + roles["curators"] + roles["testers"])
    problem_ids = [item["problem_id"] for item in items if item.get("problem_id")]
    quiz_ids = [item["quiz_id"] for item in items if item.get("quiz_id")]
    problems = list(
        Problem.objects.filter(pk__in=problem_ids).values(
            "id", "is_public", "is_organization_private"
        )
    )
    quizzes = list(Quiz.objects.filter(pk__in=quiz_ids).values("id", "is_public"))
    private_problems = [
        problem["id"]
        for problem in problems
        if not problem["is_public"] or problem["is_organization_private"]
    ]
    private_quizzes = [quiz["id"] for quiz in quizzes if not quiz["is_public"]]
    released = bool(
        contest.is_visible
        and not contest.is_private
        and not contest.is_organization_private
        and not contest.is_in_course
        and contest.start_time
        and contest.start_time <= timezone.now()
    )
    submissions = (
        Submission.objects.filter(problem_id__in=private_problems)
        .exclude(user_id__in=trusted)
        .exclude(user__user__is_superuser=True)
    )
    attempts = (
        QuizAttempt.objects.filter(quiz_id__in=private_quizzes)
        .exclude(user_id__in=trusted)
        .exclude(user__user__is_superuser=True)
    )
    # Ordinary post-release interaction is not private-content leakage. Before a
    # public scheduled contest starts, its problem set can still be confidential.
    if released:
        submissions = submissions.filter(date__lt=contest.start_time)
        attempts = attempts.filter(start_time__lt=contest.start_time)
    sub_counts = list(
        submissions.values("problem_id")
        .annotate(count=Count("id"), last_id=Max("id"), last_date=Max("date"))
        .order_by("problem_id")
    )
    attempt_counts = list(
        attempts.values("quiz_id")
        .annotate(count=Count("id"), last_id=Max("id"), last_date=Max("start_time"))
        .order_by("quiz_id")
    )
    capability = []
    if not released:
        for model, ids, target in (
            (Problem, private_problems, "problem"),
            (Quiz, private_quizzes, "quiz"),
        ):
            for role in ("authors", "curators", "testers"):
                relation = getattr(model, role).through
                queryset = (
                    relation.objects.filter(**{target + "_id__in": ids})
                    .exclude(profile_id__in=trusted)
                    .exclude(profile__user__is_superuser=True)
                )
                # Full identity evidence is retained; no per-profile query loop.
                capability.extend(
                    {
                        "kind": target,
                        "role": role,
                        "item_id": item_id,
                        "profile_id": profile_id,
                    }
                    for item_id, profile_id in queryset.values_list(
                        target + "_id", "profile_id"
                    ).order_by(target + "_id", "profile_id")
                )
    evidence = {
        "trusted_profile_ids": sorted(trusted),
        "submissions": [
            {key: _json_value(value) for key, value in row.items()}
            for row in sub_counts
        ],
        "quiz_attempts": [
            {key: _json_value(value) for key, value in row.items()}
            for row in attempt_counts
        ],
        "access_capabilities": capability,
        "capability_is_not_proof_of_reading": True,
        "public_problem_ids": [
            problem["id"]
            for problem in problems
            if problem["is_public"] and not problem["is_organization_private"]
        ],
        "material_concern": bool(sub_counts or attempt_counts or capability),
        "release_context": "public and started" if released else "not yet released",
        "limitations": [
            "No complete historical view/access log; role grants show capability, not confirmed viewing."
        ],
    }
    if detail_limit:
        # Reader-facing details use the same filtered querysets as the live
        # publication gate. They are not added to AI snapshots by default.
        # Aggregate ALL matching activity before limiting individual evidence.
        evidence["submission_groups"] = list(
            submissions.order_by()
            .values("user_id", "problem_id")
            .annotate(count=Count("id"), last_date=Max("date"))
        )
        evidence["quiz_attempt_groups"] = list(
            attempts.order_by()
            .values("user_id", "quiz_id")
            .annotate(count=Count("id"), last_date=Max("start_time"))
        )
        evidence["submission_details"] = list(
            submissions.annotate(
                access_rank=Window(
                    expression=RowNumber(),
                    partition_by=[F("user_id"), F("problem_id")],
                    order_by=[F("date").desc(), F("id").desc()],
                )
            )
            .filter(access_rank__lte=detail_limit)
            .order_by("-date", "-id")
            .values("id", "user_id", "problem_id", "date", "contest_object_id")
        )
        evidence["quiz_attempt_details"] = list(
            attempts.annotate(
                access_rank=Window(
                    expression=RowNumber(),
                    partition_by=[F("user_id"), F("quiz_id")],
                    order_by=[F("start_time").desc(), F("id").desc()],
                )
            )
            .filter(access_rank__lte=detail_limit)
            .order_by("-start_time", "-id")
            .values("id", "user_id", "quiz_id", "start_time")
        )
    return evidence
