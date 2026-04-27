import json
from unittest.mock import MagicMock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from judge.forms import ProblemAttachmentForm
from judge.models import Problem, ProblemAttachment, ProblemGroup, Profile
from judge.views.problem_data import checker_args_cleaner


class ProblemAttachmentModelTests(TestCase):
    fixtures = ["language_small"]

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("author", "a@a.com", "x")
        cls.profile, _ = Profile.objects.get_or_create(user=cls.user)
        cls.group, _ = ProblemGroup.objects.get_or_create(
            name="g", defaults={"full_name": "g"}
        )
        cls.problem = Problem.objects.create(
            code="kaggle1",
            name="Kaggle 1",
            description="",
            time_limit=1,
            memory_limit=65536,
            points=100,
            partial=True,
            group=cls.group,
        )

    def test_attachment_default_ordering(self):
        a1 = ProblemAttachment.objects.create(
            problem=self.problem,
            order=1,
            file=SimpleUploadedFile("a.txt", b"aaa"),
            description="first",
        )
        a2 = ProblemAttachment.objects.create(
            problem=self.problem,
            order=0,
            file=SimpleUploadedFile("b.txt", b"bbb"),
            description="second",
        )
        ordered = list(ProblemAttachment.objects.filter(problem=self.problem))
        self.assertEqual(ordered, [a2, a1])

    def test_cascade_delete_on_problem(self):
        ProblemAttachment.objects.create(
            problem=self.problem,
            file=SimpleUploadedFile("c.txt", b"ccc"),
        )
        self.problem.delete()
        self.assertEqual(ProblemAttachment.objects.count(), 0)


class ProblemAttachmentFormTests(TestCase):
    def test_rejects_oversize_file(self):
        big = SimpleUploadedFile("big.bin", b"\x00" * (101 * 1024 * 1024))
        form = ProblemAttachmentForm(
            data={"description": "x", "order": 0},
            files={"file": big},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("file", form.errors)

    def test_accepts_normal_file(self):
        small = SimpleUploadedFile("train.csv", b"id,x\n1,2\n")
        form = ProblemAttachmentForm(
            data={"description": "training data", "order": 0},
            files={"file": small},
        )
        self.assertTrue(form.is_valid(), form.errors)


class ProblemAttachmentViewTests(TestCase):
    fixtures = ["language_small"]

    @classmethod
    def setUpTestData(cls):
        cls.group, _ = ProblemGroup.objects.get_or_create(name="g", full_name="g")
        cls.author = User.objects.create_user("author3", "a3@a.com", "pw")
        cls.author_profile, _ = Profile.objects.get_or_create(user=cls.author)
        cls.outsider = User.objects.create_user("outsider3", "o3@o.com", "pw")
        Profile.objects.get_or_create(user=cls.outsider)
        cls.problem = Problem.objects.create(
            code="kp3",
            name="KP",
            description="",
            time_limit=1,
            memory_limit=65536,
            points=100,
            partial=True,
            group=cls.group,
        )
        cls.problem.authors.add(cls.author_profile)

    def setUp(self):
        self.client = Client()

    def _login(self, user):
        self.client.force_login(user)

    def test_tab_requires_edit_permission(self):
        url = reverse("problem_attachments", args=["kp3"])
        self._login(self.outsider)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_tab_renders_for_author(self):
        self._login(self.author)
        resp = self.client.get(reverse("problem_attachments", args=["kp3"]))
        self.assertEqual(resp.status_code, 200)

    def test_upload_creates_attachment(self):
        self._login(self.author)
        resp = self.client.post(
            reverse("problem_attachment_upload", args=["kp3"]),
            {
                "file": SimpleUploadedFile("train.csv", b"id,x\n1,2\n"),
                "description": "training data",
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(
            ProblemAttachment.objects.filter(problem=self.problem).count(), 1
        )

    def test_delete_removes_attachment(self):
        att = ProblemAttachment.objects.create(
            problem=self.problem,
            file=SimpleUploadedFile("x.csv", b"x"),
        )
        self._login(self.author)
        resp = self.client.post(
            reverse("problem_attachment_delete", args=["kp3", att.id]),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ProblemAttachment.objects.filter(id=att.id).exists())

    def test_reorder_updates_order(self):
        a1 = ProblemAttachment.objects.create(
            problem=self.problem, order=0, file=SimpleUploadedFile("1", b"1")
        )
        a2 = ProblemAttachment.objects.create(
            problem=self.problem, order=1, file=SimpleUploadedFile("2", b"2")
        )
        self._login(self.author)
        resp = self.client.post(
            reverse("problem_attachment_reorder", args=["kp3"]),
            data={"order": [str(a2.id), str(a1.id)]},
        )
        self.assertEqual(resp.status_code, 200)
        a1.refresh_from_db()
        a2.refresh_from_db()
        self.assertEqual(a2.order, 0)
        self.assertEqual(a1.order, 1)


class CheckerArgsTests(TestCase):
    def test_csv_args_round_trip(self):
        form = MagicMock()
        form.cleaned_data = {
            "checker": "csv_rmse",
            "checker_args": '{"has_header": true, "id_column": "id", "label_column": "y"}',
        }
        result = checker_args_cleaner(form)
        self.assertEqual(
            json.loads(result),
            {
                "has_header": True,
                "id_column": "id",
                "label_column": "y",
            },
        )

    def test_csv_args_no_columns_is_ok_with_defaults(self):
        # id_column and label_column are now optional — the checker aligns by
        # row index and uses the first column when omitted.
        form = MagicMock()
        form.cleaned_data = {
            "checker": "csv_rmse",
            "checker_args": '{"has_header": true}',
        }
        result = checker_args_cleaner(form)
        self.assertEqual(json.loads(result), {"has_header": True})

    def test_csv_args_empty_uses_defaults(self):
        form = MagicMock()
        form.cleaned_data = {
            "checker": "csv_rmse",
            "checker_args": "",
        }
        result = checker_args_cleaner(form)
        self.assertEqual(json.loads(result), {"has_header": True})

    def test_non_csv_args_unchanged(self):
        # Existing 'floats' precision args should still work
        form = MagicMock()
        form.cleaned_data = {
            "checker": "floats",
            "checker_args": '{"precision": 6}',
        }
        # Should not raise; current behavior returns the JSON string
        result = checker_args_cleaner(form)
        self.assertIn("precision", result)
