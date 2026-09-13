"""Attempt finalization. Call finalize_locked_attempt while holding its row lock."""

import json

from django.db import transaction
from django.utils import timezone

from judge import event_poster as event
from judge.utils.quiz_grading import auto_grade_quiz_attempt, notify_graders_for_essay


def save_submitted_answers(attempt, post_data, assignments, now=None):
    """Persist a submitted answer set with a constant number of queries."""
    now = now or timezone.now()
    model = attempt.answers.model
    existing = {answer.question_id: answer for answer in attempt.answers.all()}
    creates, updates = [], []
    for assignment in assignments:
        question = assignment.question
        key = f"q_{question.id}"
        answer = existing.get(question.id)
        if key in post_data:
            text = (
                json.dumps(post_data.getlist(key))
                if question.question_type == "MA"
                else post_data.get(key, "")
            )
        elif question.question_type == "MA":
            text = "[]"
        elif answer is not None:
            continue
        else:
            text = ""
        if answer is None:
            creates.append(
                model(attempt=attempt, question=question, answer=text, answered_at=now)
            )
        elif answer.answer != text:
            answer.answer = text
            answer.answered_at = now
            updates.append(answer)
    if creates:
        model.objects.bulk_create(creates)
        # auto_now overrides supplied values during INSERT. Preserve the time
        # captured under the attempt lock, including slow writes at the deadline.
        model.objects.filter(pk__in=[answer.pk for answer in creates]).update(
            answered_at=now
        )
    if updates:
        model.objects.bulk_update(updates, ["answer", "answered_at"])
    return list(attempt.answers.select_related("question"))


def finalize_locked_attempt(attempt, post_data=None, now=None):
    """Finalize once; expired requests cannot mutate saved answers.

    The caller must own the attempt row lock and transaction. Capture time after
    acquiring that lock, not from the browser or before waiting for a prior save.
    """
    if attempt.is_submitted:
        return False
    now = now or timezone.now()
    deadline = attempt.get_deadline()
    expired = deadline is not None and now >= deadline
    assignments = list(attempt.quiz.quiz_questions.select_related("question"))
    answers = None
    if post_data is not None and not expired:
        answers = save_submitted_answers(attempt, post_data, assignments, now)
    attempt.end_time = now
    attempt.effective_end_time = min(deadline, now) if deadline else now
    attempt.is_submitted = True
    attempt.save(update_fields=["end_time", "effective_end_time", "is_submitted"])
    auto_grade_quiz_attempt(
        attempt,
        assignments=assignments,
        answers=answers,
        sync_contest=False,
        preserve_manual=expired or post_data is None,
    )
    if attempt.contest_participation_id:
        # Derived DB results commit with the attempt; failures remain retryable.
        participation = attempt.contest_participation
        participation.recompute_results()
        contest = participation.contest
        if contest.scoreboard_visibility == contest.SCOREBOARD_VISIBLE:
            transaction.on_commit(
                lambda: event.post(
                    "contest_%s" % contest.key, {"type": "ranking-update"}
                ),
                robust=True,
            )
    transaction.on_commit(lambda: notify_graders_for_essay(attempt), robust=True)
    return expired
