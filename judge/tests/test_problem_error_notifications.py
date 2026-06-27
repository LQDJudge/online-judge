from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from judge.bridge.judge_handler import JudgeHandler
from judge.models import Language, Notification, Problem, Profile, Submission
from judge.models.notification import NotificationCategory
from judge.utils.problem_data import notify_problem_authors


def _make_language():
    return Language.objects.get_or_create(
        key="PY3",
        defaults={
            "name": "Python 3",
            "short_name": "PY3",
            "common_name": "Python",
            "ace": "python",
            "pygments": "python3",
            "template": "",
            "extension": "py",
        },
    )[0]


@override_settings(LANGUAGE_CODE="en")
class ProblemErrorNotificationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language = _make_language()
        cls.author_user = User.objects.create_user("owner", "", "pw")
        cls.author, _ = Profile.objects.get_or_create(
            user=cls.author_user, defaults={"language": cls.language}
        )
        cls.curator_user = User.objects.create_user("curator", "", "pw")
        cls.curator, _ = Profile.objects.get_or_create(
            user=cls.curator_user, defaults={"language": cls.language}
        )
        cls.submitter_user = User.objects.create_user("submitter", "", "pw")
        cls.submitter, _ = Profile.objects.get_or_create(
            user=cls.submitter_user, defaults={"language": cls.language}
        )

    def _make_problem(self, code="perr"):
        return Problem.objects.create(
            code=code,
            name="Problem error",
            description="x",
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )

    def _make_submission(self, problem):
        return Submission.objects.create(
            user=self.submitter,
            problem=problem,
            language=self.language,
        )

    def test_internal_error_notifies_author_in_app_without_email(self):
        problem = self._make_problem()
        problem.authors.add(self.author)
        problem.curators.add(self.curator)
        submission = self._make_submission(problem)
        error_message = (
            "Traceback\n" "dmoj.error.InternalError: generator timed out (> 20 seconds)"
        )

        with patch("judge.utils.problem_data.log_exception") as log_exception:
            JudgeHandler._notify_problem_authors_on_error(
                SimpleNamespace(name="judge2"), submission.id, error_message
            )

        log_exception.assert_not_called()
        notification = Notification.objects.get(
            owner=self.author,
            category=NotificationCategory.PROBLEM,
        )
        self.assertTrue(
            Notification.objects.filter(
                owner=self.curator,
                category=NotificationCategory.PROBLEM,
            ).exists()
        )
        self.assertIn("generator timed out", notification.html_link)
        self.assertIn(
            reverse("problem_data", args=[problem.code]), notification.html_link
        )
        self.assertEqual(notification.extra_data["problem_code"], problem.code)
        self.assertEqual(notification.extra_data["submission_id"], submission.id)

    def test_internal_error_without_problem_owner_keeps_admin_fallback(self):
        problem = self._make_problem("perrnone")
        submission = self._make_submission(problem)

        with patch("judge.utils.problem_data.log_exception") as log_exception:
            JudgeHandler._notify_problem_authors_on_error(
                SimpleNamespace(name="judge2"),
                submission.id,
                "dmoj.error.InternalError: generator timed out",
            )

        self.assertFalse(Notification.objects.exists())
        log_exception.assert_called()

    def test_internal_error_falls_back_when_in_app_notification_fails(self):
        problem = self._make_problem("perrfail")
        problem.authors.add(self.author)
        submission = self._make_submission(problem)

        with patch(
            "judge.bridge.judge_handler._notify_problem_owners_in_app",
            side_effect=RuntimeError("notification write failed"),
        ), patch("judge.utils.problem_data.log_exception") as log_exception:
            JudgeHandler._notify_problem_authors_on_error(
                SimpleNamespace(name="judge2"),
                submission.id,
                "dmoj.error.InternalError: generator timed out",
            )

        log_exception.assert_called()

    def test_problem_owner_email_includes_curators(self):
        problem = self._make_problem("perremail")
        curator_user = User.objects.create_user("emailcurator", "c@example.com", "pw")
        curator, _ = Profile.objects.get_or_create(
            user=curator_user, defaults={"language": self.language}
        )
        problem.authors.add(self.author)
        problem.curators.add(curator)

        with patch("judge.utils.problem_data.send_mail") as send_mail, patch(
            "judge.utils.problem_data.log_exception"
        ) as log_exception:
            notify_problem_authors(
                problem,
                "curator email test",
                error_type="Judge Internal Error",
            )

        log_exception.assert_not_called()
        send_mail.assert_called_once()
        self.assertEqual(
            send_mail.call_args.kwargs["recipient_list"], ["c@example.com"]
        )
