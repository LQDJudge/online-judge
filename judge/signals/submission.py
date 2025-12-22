from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from judge.models import Submission, ContestSubmission
from judge.utils.problems import finished_submission


@receiver(post_delete, sender=Submission)
def submission_delete(sender, instance, **kwargs):
    finished_submission(instance)
    instance.user.calculate_points()


@receiver(post_delete, sender=ContestSubmission)
def contest_submission_delete(sender, instance, **kwargs):
    participation = instance.participation
    participation.recompute_results()


@receiver(post_save, sender=ContestSubmission)
def contest_submission_update(sender, instance, **kwargs):
    Submission.objects.filter(id=instance.submission_id).update(
        contest_object_id=instance.participation.contest_id
    )
