from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from judge.utils.deferred_email import deferred_send_mail
from judge.views.email import DeferredPasswordResetForm


class DeferredSendMailTest(TestCase):
    @override_settings(
        DEFERRED_EMAIL_TASK_PRIORITY=3,
        DEFERRED_EMAIL_TASK_QUEUE="email_default",
    )
    def test_deferred_send_mail_enqueues_after_commit(self):
        with patch(
            "judge.utils.deferred_email.send_mail_task.apply_async"
        ) as apply_async:
            with self.captureOnCommitCallbacks(execute=True):
                result = deferred_send_mail(
                    "Subject",
                    "Body",
                    "from@example.com",
                    ("to@example.com",),
                    html_message="<p>Body</p>",
                )

        self.assertIsNone(result)
        apply_async.assert_called_once()
        self.assertEqual(apply_async.call_args.kwargs["priority"], 3)
        self.assertEqual(apply_async.call_args.kwargs["queue"], "email_default")
        self.assertEqual(
            apply_async.call_args.kwargs["args"],
            (
                "Subject",
                "Body",
                "from@example.com",
                ["to@example.com"],
                False,
                None,
                None,
                "<p>Body</p>",
            ),
        )

    def test_deferred_send_mail_can_send_synchronously(self):
        with patch(
            "judge.utils.deferred_email.django_send_mail", return_value=1
        ) as send_mail:
            result = deferred_send_mail(
                "Subject",
                "Body",
                "from@example.com",
                ["to@example.com"],
                defer=False,
            )

        self.assertEqual(result, 1)
        send_mail.assert_called_once_with(
            "Subject",
            "Body",
            "from@example.com",
            ["to@example.com"],
            fail_silently=False,
            auth_user=None,
            auth_password=None,
            connection=None,
            html_message=None,
        )

    def test_deferred_send_mail_rejects_custom_connection(self):
        with self.assertRaises(ValueError):
            deferred_send_mail(
                "Subject",
                "Body",
                "from@example.com",
                ["to@example.com"],
                connection=object(),
            )


@override_settings(LANGUAGE_CODE="en", SITE_NAME="LQDOJ")
class DeferredPasswordResetFormTest(TestCase):
    def test_password_reset_form_uses_deferred_email(self):
        user = User.objects.create_user(
            username="reset_user",
            email="reset@example.com",
            password="pw",
        )
        context = {
            "domain": "example.com",
            "site_name": "LQDOJ",
            "uid": "uid-token",
            "user": user,
            "token": "reset-token",
            "protocol": "https",
        }

        with patch("judge.views.email.deferred_send_mail") as send_mail:
            DeferredPasswordResetForm().send_mail(
                "registration/password_reset_subject.txt",
                "registration/password_reset_email.txt",
                context,
                "from@example.com",
                user.email,
                "registration/password_reset_email.html",
            )

        send_mail.assert_called_once()
        subject, body, from_email, recipients = send_mail.call_args.args
        self.assertIn("Password reset", subject)
        self.assertIn("https://example.com", body)
        self.assertEqual(from_email, "from@example.com")
        self.assertEqual(recipients, [user.email])
        self.assertIn("html_message", send_mail.call_args.kwargs)
        self.assertEqual(send_mail.call_args.kwargs["priority"], 8)
