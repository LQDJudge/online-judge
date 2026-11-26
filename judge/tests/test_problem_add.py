from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse

from judge.models import Language, Problem, ProblemGroup, Profile
from judge.views.problem import ProblemAdd


@override_settings(LANGUAGE_CODE="en")
class ProblemAddTest(TestCase):
    fixtures = ["language_small"]

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("problem_creator", password="pw")
        cls.profile = Profile.objects.create(
            user=cls.user, language=Language.objects.first()
        )
        cls.group = ProblemGroup.objects.create(name="addtest", full_name="Add test")

    def setUp(self):
        self.client.force_login(self.user)
        self.payload = {
            "code": "32132",
            "name": "New problem",
            "group": self.group.pk,
            "authors": [self.profile.pk],
            "time_limit": 1,
            "memory_limit": 64,
            "memory_unit": "MB",
            "points": 1,
            "allowed_languages": [self.profile.language_id],
        }

    def test_successful_creation_and_repeated_submission(self):
        response = self.client.post(reverse("problem_add"), self.payload)
        self.assertEqual(response.status_code, 302)
        problem = Problem.objects.get(code="32132")
        self.assertEqual(list(problem.authors.all()), [self.profile])

        response = self.client.post(reverse("problem_add"), self.payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("code", response.context["form"].errors)
        self.assertEqual(Problem.objects.filter(code="32132").count(), 1)

    def test_duplicate_created_after_validation_returns_field_error(self):
        original_form_valid = ProblemAdd.form_valid

        def create_competing_problem(view, form):
            # A different request wins after this form's uniqueness checks.
            Problem.objects.create(
                code=form.cleaned_data["code"],
                name="Existing winner",
                group=self.group,
                time_limit=1,
                memory_limit=65536,
                points=1,
            )
            return original_form_valid(view, form)

        with patch.object(ProblemAdd, "form_valid", create_competing_problem):
            response = self.client.post(reverse("problem_add"), self.payload)

        self.assertEqual(response.status_code, 200)
        self.assertIn("code", response.context["form"].errors)
        self.assertEqual(response.context["form"].data["name"], "New problem")
        problem = Problem.objects.get(code="32132")
        self.assertEqual(problem.name, "Existing winner")
        self.assertFalse(problem.authors.exists())
        self.assertEqual(Problem.objects.filter(code="32132").count(), 1)

    def test_unrelated_integrity_error_is_not_hidden(self):
        with patch.object(
            Problem, "save", side_effect=IntegrityError("other constraint")
        ):
            with self.assertRaisesMessage(IntegrityError, "other constraint"):
                self.client.post(reverse("problem_add"), self.payload)
        self.assertFalse(Problem.objects.filter(code="32132").exists())
