"""
Quiz Grading Utilities

This module contains grading algorithms for different quiz question types.
"""

import json
import logging
import math
import re
from typing import Tuple

from django.apps import apps
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from judge import event_poster as event

logger = logging.getLogger("judge.quiz_grading")


def normalize_sa(s, case_sensitive=False):
    """Normalize a short-answer string for comparison.

    Touches ONLY whitespace and case — never meaning-bearing characters.
    Commas, dots, digits, letters, brackets and operators are preserved exactly,
    so "1,2", "1.2", "12" and "1 2" all stay distinct (critical for math/CP).
    Forgives: case, leading/trailing/repeated whitespace, and spaces adjacent to
    punctuation ("5, 8" == "5,8", "Chloe: 5" == "Chloe:5"). It never removes or
    alters punctuation itself, and never removes a space between two alphanumerics
    (so "1 2" != "12").
    """
    s = "" if s is None else str(s)  # tolerate non-string entries (e.g. numbers)
    if not case_sensitive:
        s = s.lower()
    s = s.strip()
    s = re.sub(r"\s+", " ", s)  # collapse whitespace runs to a single space
    s = re.sub(r"\s*([^\w\s])\s*", r"\1", s)  # drop spaces adjacent to punctuation
    return s


def sa_exact_match(text, answers, case_sensitive=False):
    """True if `text` normalizes equal to ANY entry in `answers` (logical OR)."""
    normalized = normalize_sa(text, case_sensitive)
    return any(normalized == normalize_sa(a, case_sensitive) for a in answers)


def grade_multiple_choice(answer, max_points=None) -> Tuple[float, bool]:
    """
    Grade MC/TF question - single correct choice.

    Args:
        answer: QuizAnswer instance

    Returns:
        Tuple of (points_earned, is_correct)
    """
    question = answer.question
    correct_answers = question.correct_answers

    if not correct_answers:
        return (0, False)

    correct_id = correct_answers.get("answers")
    selected_id = answer.answer

    is_correct = selected_id == correct_id

    if max_points is None:
        try:
            assignment_model = answer.attempt.quiz.quiz_questions.model
            assignment = assignment_model.objects.get(
                quiz=answer.attempt.quiz, question=question
            )
            max_points = assignment.points
        except assignment_model.DoesNotExist:
            max_points = 1.0

    points = max_points if is_correct else 0

    return (points, is_correct)


def grade_multiple_answer(answer, max_points=None) -> Tuple[float, bool]:
    """
    Grade MA question using the configured grading strategy.

    Grading Strategies:
    - all_or_nothing: Full points only if exact match (default)
    - partial_credit: Proportional credit with penalty for wrong answers
    - right_minus_wrong: +1 for each correct, -1 for each wrong (normalized)
    - correct_only: Points for correct answers, no penalty for wrong

    Args:
        answer: QuizAnswer instance

    Returns:
        Tuple of (points_earned, is_correct)
    """
    question = answer.question
    correct_answers = question.correct_answers

    if not correct_answers:
        return (0, False)

    correct_ids = set(correct_answers.get("answers", []))

    # Parse selected answers (stored as JSON string)
    selected_ids = set()
    if answer.answer:
        try:
            if isinstance(answer.answer, str):
                selected_ids = set(json.loads(answer.answer))
            elif isinstance(answer.answer, list):
                selected_ids = set(answer.answer)
        except (json.JSONDecodeError, TypeError):
            pass

    # Get all choice IDs to determine wrong choices
    all_choice_ids = set(c["id"] for c in (question.choices or []))
    wrong_ids = all_choice_ids - correct_ids

    # Calculate hits and misses
    correct_selected = len(selected_ids & correct_ids)
    wrong_selected = len(selected_ids & wrong_ids)
    total_correct = len(correct_ids)
    total_wrong = len(wrong_ids)

    if max_points is None:
        try:
            assignment_model = answer.attempt.quiz.quiz_questions.model
            assignment = assignment_model.objects.get(
                quiz=answer.attempt.quiz, question=question
            )
            max_points = assignment.points
        except assignment_model.DoesNotExist:
            max_points = 1.0

    # Get grading strategy (default to all_or_nothing for backwards compatibility)
    strategy = (
        getattr(question, "grading_strategy", "all_or_nothing") or "all_or_nothing"
    )

    # Apply grading strategy
    if strategy == "all_or_nothing":
        score_ratio = 1.0 if selected_ids == correct_ids else 0.0

    elif strategy == "partial_credit":
        # Proportional credit with penalty
        # Formula: (correct_selected / total_correct) - (wrong_selected / total_wrong)
        if total_correct == 0:
            score_ratio = 0.0
        else:
            credit = correct_selected / total_correct
            penalty = (wrong_selected / total_wrong) if total_wrong > 0 else 0
            score_ratio = max(0, credit - penalty)

    elif strategy == "right_minus_wrong":
        # +1 for each correct, -1 for each wrong (normalized to total_correct)
        if total_correct == 0:
            score_ratio = 0.0
        else:
            net_correct = correct_selected - wrong_selected
            score_ratio = max(0, net_correct / total_correct)

    elif strategy == "correct_only":
        # Points only for correct answers, no penalty for wrong
        if total_correct == 0:
            score_ratio = 0.0
        else:
            score_ratio = correct_selected / total_correct

    else:
        # Fallback to all_or_nothing
        score_ratio = 1.0 if selected_ids == correct_ids else 0.0

    points = max_points * score_ratio
    is_correct = score_ratio == 1.0

    return (points, is_correct)


def grade_short_answer(answer, max_points=None) -> Tuple[float, bool, bool]:
    """
    Grade SA question - match against patterns.

    Args:
        answer: QuizAnswer instance

    Returns:
        Tuple of (points_earned, is_correct, needs_manual_grading)
    """
    question = answer.question
    correct_answers = question.correct_answers
    text = (answer.answer or "").strip()

    if not correct_answers:
        return (0, False, True)  # Needs manual grading

    config = correct_answers
    case_sensitive = config.get("case_sensitive", False)
    answer_type = config.get("type", "exact")
    answers = config.get("answers", [])

    if not answers:
        return (0, False, True)

    is_correct = False

    if answer_type == "exact":
        # Normalized exact: forgives whitespace/case, never touches meaning.
        is_correct = sa_exact_match(text, answers, case_sensitive)

    elif answer_type == "regex":
        # Legacy: retained for old questions; no longer creatable in the UI.
        flags = 0 if case_sensitive else re.IGNORECASE
        for pattern in answers:
            try:
                if re.match(pattern, text, flags):
                    is_correct = True
                    break
            except re.error:
                continue

    if max_points is None:
        try:
            assignment_model = answer.attempt.quiz.quiz_questions.model
            assignment = assignment_model.objects.get(
                quiz=answer.attempt.quiz, question=question
            )
            max_points = assignment.points
        except assignment_model.DoesNotExist:
            max_points = 1.0

    points = max_points if is_correct else 0

    # If correct, no manual review needed
    # If incorrect but has non-empty answer, flag for manual review
    # (teacher may want to give partial credit or the pattern was too strict)
    needs_manual = not is_correct and bool(text)

    return (points, is_correct, needs_manual)


def grade_essay(answer) -> Tuple[float, bool, bool]:
    """
    Grade ES question - always needs manual grading.

    Args:
        answer: QuizAnswer instance

    Returns:
        Tuple of (points_earned, is_correct, needs_manual_grading)
    """
    # Essay questions always need manual grading
    return (0, False, True)


def grade_answer(answer, max_points=None) -> Tuple[float, bool, bool]:
    """
    Grade a single answer based on question type.

    Args:
        answer: QuizAnswer instance

    Returns:
        Tuple of (points_earned, is_correct, needs_manual_grading)
    """
    qtype = answer.question.question_type

    if qtype in ("MC", "TF"):
        points, is_correct = grade_multiple_choice(answer, max_points)
        return (points, is_correct, False)

    elif qtype == "MA":
        points, is_correct = grade_multiple_answer(answer, max_points)
        return (points, is_correct, False)

    elif qtype == "SA":
        return grade_short_answer(answer, max_points)

    elif qtype == "ES":
        return grade_essay(answer)

    return (0, False, True)


def auto_grade_answer(answer) -> bool:
    """
    Auto-grade a single answer and save the result.

    Args:
        answer: QuizAnswer instance

    Returns:
        True if grading was performed (even if incorrect),
        False if manual grading is needed
    """
    points, is_correct, needs_manual = grade_answer(answer)

    if needs_manual and answer.question.question_type == "ES":
        # Essay questions - don't mark as graded
        return False

    answer.points = points
    answer.is_correct = is_correct
    answer.partial_credit = 1.0 if is_correct else 0.0
    answer.graded_at = timezone.now()
    answer.save(update_fields=["points", "is_correct", "partial_credit", "graded_at"])

    return True


def auto_grade_quiz_attempt(attempt, assignments=None, answers=None) -> float:
    """
    Auto-grade all answers in an attempt.

    Args:
        attempt: QuizAttempt instance

    Returns:
        Total score achieved
    """
    if assignments is None:
        assignments = list(attempt.quiz.quiz_questions.select_related("question"))
    else:
        assignments = list(assignments)

    if answers is None:
        answers = list(attempt.answers.select_related("question"))
    else:
        answers = list(answers)

    assignment_points = {
        assignment.question_id: assignment.points for assignment in assignments
    }
    graded_at = timezone.now()
    total_score = 0

    for answer in answers:
        qtype = answer.question.question_type
        max_points = assignment_points.get(answer.question_id, 1.0)

        if qtype in ("MC", "TF"):
            points, is_correct = grade_multiple_choice(answer, max_points)
            answer.points = points
            answer.is_correct = is_correct
            answer.partial_credit = 1.0 if is_correct else 0.0
            answer.graded_at = graded_at

        elif qtype == "MA":
            points, is_correct = grade_multiple_answer(answer, max_points)
            answer.points = points
            answer.is_correct = is_correct
            answer.partial_credit = 1.0 if is_correct else 0.0
            answer.graded_at = graded_at

        elif qtype == "SA":
            points, is_correct, needs_manual = grade_short_answer(answer, max_points)
            answer.points = points
            answer.is_correct = is_correct
            answer.partial_credit = 1.0 if is_correct else 0.0
            # Always mark as graded — wrong answers get 0 points,
            # teacher can manually adjust via grading dashboard if needed
            answer.graded_at = graded_at

        elif qtype == "ES":
            # Essay always needs manual grading
            answer.points = 0
            answer.is_correct = False
            answer.partial_credit = 0.0
            # Don't set graded_at - needs manual review

        total_score += answer.points

    with transaction.atomic(savepoint=False):
        if answers:
            attempt.answers.model.objects.bulk_update(
                answers,
                ["points", "is_correct", "partial_credit", "graded_at"],
            )

        max_score = sum(assignment.points for assignment in assignments)

        attempt.score = total_score
        attempt.max_score = max_score
        attempt.save(update_fields=["score", "max_score"])
        sync_quiz_attempt_result(attempt, sync_contest=False)

    if attempt.is_submitted and attempt.contest_participation_id:
        sync_contest_quiz_result(attempt.contest_participation, attempt.id)

    return total_score


def sync_contest_quiz_result(participation, attempt_id=None):
    """Recompute one contest participation and notify its live scoreboard."""
    try:
        participation.recompute_results()
        contest_model = apps.get_model("judge", "Contest")
        contest = participation.contest
        if contest.scoreboard_visibility == contest_model.SCOREBOARD_VISIBLE:
            event.post(
                "contest_%s" % contest.key,
                {"type": "ranking-update"},
            )
    except Exception:
        logger.exception(
            "Failed to synchronize contest result for quiz attempt %s",
            attempt_id,
        )


def sync_quiz_attempt_result(attempt, sync_contest=True):
    """Synchronize every result derived from a submitted quiz attempt."""
    if not attempt.is_submitted:
        return

    if sync_contest and attempt.contest_participation_id:
        sync_contest_quiz_result(attempt.contest_participation, attempt.id)

    # Update best quiz attempt cache for course lesson grade tracking
    best_attempt_model = apps.get_model("judge", "BestQuizAttempt")
    best_attempt_model.update_from_attempt(attempt)


def validate_manual_answer_points(raw_points, max_points):
    """Return a valid manual score or raise ValueError."""
    try:
        points = float(raw_points)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid points value") from exc

    if not math.isfinite(points) or points < 0 or points > float(max_points):
        raise ValueError("Points must be between 0 and the question maximum")

    return points


def calculate_attempt_score(attempt) -> Tuple[float, float]:
    """
    Calculate the total score and max score for an attempt.

    Args:
        attempt: QuizAttempt instance

    Returns:
        Tuple of (score, max_score)
    """
    total_score = 0
    max_score = 0

    assignments = attempt.quiz.quiz_questions.all()
    assignment_points = {a.question_id: a.points for a in assignments}

    for answer in attempt.answers.all():
        total_score += answer.points or 0
        max_score += assignment_points.get(answer.question_id, 1.0)  # Default fallback

    return (total_score, max_score)


def notify_graders_for_essay(attempt):
    """
    Send notifications to quiz authors and curators when essay answers need grading.

    Args:
        attempt: QuizAttempt instance
    """
    # Check if there are essay questions that need grading
    has_essays = attempt.answers.filter(
        question__question_type="ES",
        graded_at__isnull=True,
    ).exists()

    if not has_essays:
        return

    quiz = attempt.quiz
    student_username = attempt.user.user.username

    # Get all graders (authors and curators)
    grader_ids = set()
    for author in quiz.authors.all():
        grader_ids.add(author.id)
    for curator in quiz.curators.all():
        grader_ids.add(curator.id)

    if not grader_ids:
        return

    # Create notification link
    grade_url = reverse("attempt_grade", args=[attempt.id])
    html_link = f'<a href="{grade_url}">{quiz.title}</a>'

    # Send notifications
    notification_model = apps.get_model("judge", "Notification")
    notification_model.objects.bulk_create_notifications(
        user_ids=list(grader_ids),
        category="quiz_needs_grading",
        html_link=html_link,
        author=attempt.user,
        extra_data={
            "quiz_code": quiz.code,
            "quiz_title": quiz.title,
            "student_username": student_username,
            "attempt_id": attempt.id,
        },
        deduplicate=True,
    )
