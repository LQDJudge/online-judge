from django.db.models.signals import post_delete
from django.dispatch import receiver

from judge.models import BestQuizAttempt, ContestParticipation, QuizAttempt
from judge.utils.quiz_grading import sync_contest_quiz_result


@receiver(post_delete, sender=QuizAttempt)
def quiz_attempt_delete(sender, instance, **kwargs):
    if instance.lesson_quiz_id:
        BestQuizAttempt.recalculate_for_user_lesson_quiz(
            instance.user_id, instance.lesson_quiz_id
        )

    if instance.contest_participation_id:
        participation = ContestParticipation.objects.filter(
            id=instance.contest_participation_id
        ).first()
        if participation:
            sync_contest_quiz_result(participation, instance.id)
