"""Anonymous users on login-required quiz pages are redirected to login.

The quiz access mixins (QuizEditorMixin, QuestionEditorMixin, QuizObjectEditorMixin)
used to override handle_no_permission to ALWAYS raise PermissionDenied (403), which
broke the standard "redirect anonymous to the login page" behaviour. They now only
403 authenticated-but-unauthorized users, and let anonymous users fall through to
AccessMixin's login redirect.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from judge.models import Language, Profile
from judge.models.quiz import Quiz, QuizQuestion


class QuizAccessRedirectTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        lang = Language.objects.first()
        user = User.objects.create_user("plainuser", "p@p.com", "pw")
        self.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": lang}
        )
        # No authors -> plainuser is NOT an editor of these objects.
        self.quiz = Quiz.objects.create(code="accquiz", title="Acc Quiz")
        self.question = QuizQuestion.objects.create(
            question_type="MC",
            title="Q",
            content="?",
            choices=[{"id": "a", "text": "1"}],
            correct_answers={"answers": "a"},
        )

    def _assert_login_redirect(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)
        self.assertIn("next=", resp.url)

    # --- QuizEditorMixin (grading dashboard: any authenticated user) ---
    def test_dashboard_anonymous_redirects_to_login(self):
        self._assert_login_redirect(reverse("grading_dashboard"))

    def test_dashboard_authenticated_ok(self):
        self.client.force_login(self.profile.user)
        self.assertEqual(self.client.get(reverse("grading_dashboard")).status_code, 200)

    # --- QuizObjectEditorMixin (quiz edit) ---
    def test_quiz_edit_anonymous_redirects_to_login(self):
        self._assert_login_redirect(reverse("quiz_edit", args=[self.quiz.code]))

    def test_quiz_edit_authenticated_non_editor_forbidden(self):
        self.client.force_login(self.profile.user)
        self.assertEqual(
            self.client.get(reverse("quiz_edit", args=[self.quiz.code])).status_code,
            403,
        )

    # --- QuestionEditorMixin (question edit) ---
    def test_question_edit_anonymous_redirects_to_login(self):
        self._assert_login_redirect(
            reverse("question_bank_edit", args=[self.question.pk])
        )

    def test_question_edit_authenticated_non_editor_forbidden(self):
        self.client.force_login(self.profile.user)
        self.assertEqual(
            self.client.get(
                reverse("question_bank_edit", args=[self.question.pk])
            ).status_code,
            403,
        )
