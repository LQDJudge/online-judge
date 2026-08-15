from django.conf import settings
from django.core.mail import send_mail as django_send_mail
from django.db import transaction

from celery import shared_task

DEFAULT_EMAIL_TASK_PRIORITY = 5


@shared_task
def send_mail_task(
    subject,
    message,
    from_email,
    recipient_list,
    fail_silently=False,
    auth_user=None,
    auth_password=None,
    html_message=None,
):
    return django_send_mail(
        subject,
        message,
        from_email,
        recipient_list,
        fail_silently=fail_silently,
        auth_user=auth_user,
        auth_password=auth_password,
        html_message=html_message,
    )


def _email_task_options(priority, queue):
    if priority is None:
        priority = getattr(
            settings, "DEFERRED_EMAIL_TASK_PRIORITY", DEFAULT_EMAIL_TASK_PRIORITY
        )
    if queue is None:
        queue = getattr(settings, "DEFERRED_EMAIL_TASK_QUEUE", None)

    task_options = {}
    if priority is not None:
        task_options["priority"] = priority
    if queue:
        task_options["queue"] = queue
    return task_options


def deferred_send_mail(
    subject,
    message,
    from_email,
    recipient_list,
    fail_silently=False,
    auth_user=None,
    auth_password=None,
    connection=None,
    html_message=None,
    defer=True,
    priority=None,
    queue=None,
):
    if not defer:
        return django_send_mail(
            subject,
            message,
            from_email,
            recipient_list,
            fail_silently=fail_silently,
            auth_user=auth_user,
            auth_password=auth_password,
            connection=connection,
            html_message=html_message,
        )
    if connection is not None:
        raise ValueError("deferred_send_mail cannot defer a custom email connection")

    recipient_list = list(recipient_list)
    task_kwargs = {
        "args": (
            subject,
            message,
            from_email,
            recipient_list,
            fail_silently,
            auth_user,
            auth_password,
            html_message,
        ),
        **_email_task_options(priority, queue),
    }
    transaction.on_commit(lambda: send_mail_task.apply_async(**task_kwargs))
    return None
