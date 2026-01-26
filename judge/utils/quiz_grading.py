"""
Quiz Grading Utilities

This module contains grading algorithms for different quiz question types.
"""

import json
import re
from typing import Tuple

from django.urls import reverse
from django.utils import timezone


def grade_multiple_choice(answer) -> Tuple[float, bool]:
    """
    Grade MC/TF question - single correct choice.

    Args:
        answer: QuizAnswer instance

    Returns:
        Tuple of (points_earned, is_correct)
    """
    from judge.models.quiz import QuizQuestionAssignment

    question = answer.question
    correct_answers = question.correct_answers

    if not correct_answers:
        return (0, False)

    correct_id = correct_answers.get("answers")
    selected_id = answer.answer

    is_correct = selected_id == correct_id

    # Get points from assignment
    try:
        assignment = QuizQuestionAssignment.objects.get(
            quiz=answer.attempt.quiz, question=question
        )
        points = assignment.points if is_correct else 0
    except QuizQuestionAssignment.DoesNotExist:
        points = 1.0 if is_correct else 0  # Default fallback

    return (points, is_correct)


def grade_multiple_answer(answer) -> Tuple[float, bool]:
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
    from judge.models.quiz import QuizQuestionAssignment

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

    # Get max points from assignment
    try:
        assignment = QuizQuestionAssignment.objects.get(
            quiz=answer.attempt.quiz, question=question
        )
        max_points = assignment.points
    except QuizQuestionAssignment.DoesNotExist:
        max_points = 1.0  # Default fallback

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


def grade_short_answer(answer) -> Tuple[float, bool, bool]:
    """
    Grade SA question - match against patterns.

    Args:
        answer: QuizAnswer instance

    Returns:
        Tuple of (points_earned, is_correct, needs_manual_grading)
    """
    from judge.models.quiz import QuizQuestionAssignment

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

    compare_text = text if case_sensitive else text.lower()

    is_correct = False

    if answer_type == "exact":
        compare_answers = answers if case_sensitive else [a.lower() for a in answers]
        is_correct = compare_text in compare_answers

    elif answer_type == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        for pattern in answers:
            try:
                if re.match(pattern, text, flags):
                    is_correct = True
                    break
            except re.error:
                continue

    # Get points from assignment
    try:
        assignment = QuizQuestionAssignment.objects.get(
            quiz=answer.attempt.quiz, question=question
        )
        points = assignment.points if is_correct else 0
    except QuizQuestionAssignment.DoesNotExist:
        points = 1.0 if is_correct else 0  # Default fallback

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


def grade_answer(answer) -> Tuple[float, bool, bool]:
    """
    Grade a single answer based on question type.

    Args:
        answer: QuizAnswer instance

    Returns:
        Tuple of (points_earned, is_correct, needs_manual_grading)
    """
    qtype = answer.question.question_type

    if qtype in ("MC", "TF"):
        points, is_correct = grade_multiple_choice(answer)
        return (points, is_correct, False)

    elif qtype == "MA":
        points, is_correct = grade_multiple_answer(answer)
        return (points, is_correct, False)

    elif qtype == "SA":
        return grade_short_answer(answer)

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


def auto_grade_quiz_attempt(attempt) -> float:
    """
    Auto-grade all answers in an attempt.

    Args:
        attempt: QuizAttempt instance

    Returns:
        Total score achieved
    """
    total_score = 0

    for answer in attempt.answers.all():
        qtype = answer.question.question_type

        if qtype in ("MC", "TF"):
            points, is_correct = grade_multiple_choice(answer)
            answer.points = points
            answer.is_correct = is_correct
            answer.partial_credit = 1.0 if is_correct else 0.0
            answer.graded_at = timezone.now()

        elif qtype == "MA":
            points, is_correct = grade_multiple_answer(answer)
            answer.points = points
            answer.is_correct = is_correct
            answer.partial_credit = 1.0 if is_correct else 0.0
            answer.graded_at = timezone.now()

        elif qtype == "SA":
            points, is_correct, needs_manual = grade_short_answer(answer)
            answer.points = points
            answer.is_correct = is_correct
            answer.partial_credit = 1.0 if is_correct else 0.0
            # Always mark as graded if correct_answers are configured
            # Wrong answers get 0 points - teacher can manually adjust if needed
            if answer.question.correct_answers:
                answer.graded_at = timezone.now()

        elif qtype == "ES":
            # Essay always needs manual grading
            answer.points = 0
            answer.is_correct = False
            answer.partial_credit = 0.0
            # Don't set graded_at - needs manual review

        answer.save()
        total_score += answer.points

    # Calculate max score from assignments
    from judge.models.quiz import QuizQuestionAssignment

    max_score = 0
    assignments = QuizQuestionAssignment.objects.filter(quiz=attempt.quiz)
    for assignment in assignments:
        max_score += assignment.points

    # Update attempt score and max_score
    attempt.score = total_score
    attempt.max_score = max_score
    attempt.save(update_fields=["score", "max_score"])

    # Update contest participation if applicable
    if hasattr(attempt, "contest_participation") and attempt.contest_participation:
        try:
            attempt.contest_participation.recompute_results()
        except Exception:
            pass

    # Update best quiz attempt cache for course lesson grade tracking
    from judge.models import BestQuizAttempt

    BestQuizAttempt.update_from_attempt(attempt)

    return total_score


def calculate_attempt_score(attempt) -> Tuple[float, float]:
    """
    Calculate the total score and max score for an attempt.

    Args:
        attempt: QuizAttempt instance

    Returns:
        Tuple of (score, max_score)
    """
    from judge.models.quiz import QuizQuestionAssignment

    total_score = 0
    max_score = 0

    assignments = QuizQuestionAssignment.objects.filter(quiz=attempt.quiz)
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
    from judge.models.notification import Notification, NotificationCategory

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
    Notification.objects.bulk_create_notifications(
        user_ids=list(grader_ids),
        category=NotificationCategory.QUIZ_NEEDS_GRADING,
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
