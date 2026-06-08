from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from judge.models import Language, Problem, ProblemGroup, Profile
from judge.models.problem_data import ProblemData, ProblemSolutionCode
from judge.review.hashing import compute_input_hash


class ComputeInputHashTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python 3",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
                "template": "",
            },
        )
        cls.problem_group, _ = ProblemGroup.objects.get_or_create(
            name="TG", defaults={"full_name": "Test Group"}
        )
        user = User.objects.create_user("hasher", "h@h.com", "x")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="rhash1",
            name="Hashing Test",
            description="A problem.",
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        # Authors M2M is required by Problem.save()
        cls.problem.authors.add(cls.profile)

    def test_stable_for_unchanged_problem(self):
        h1 = compute_input_hash(self.problem)
        h2 = compute_input_hash(self.problem)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_changes_when_description_changes(self):
        h1 = compute_input_hash(self.problem)
        self.problem.description = "Different statement."
        self.problem.save()
        h2 = compute_input_hash(self.problem)
        self.assertNotEqual(h1, h2)

    def test_changes_when_time_limit_changes(self):
        h1 = compute_input_hash(self.problem)
        self.problem.time_limit = 2.0
        self.problem.save()
        h2 = compute_input_hash(self.problem)
        self.assertNotEqual(h1, h2)

    def test_changes_when_custom_checker_content_changes_same_filename(self):
        # Regression guard: swapping the bytes inside a custom checker file
        # while keeping the same filename used to slip past the dirty-check
        # because we only hashed the filename. Now content is hashed too.
        pd, _ = ProblemData.objects.get_or_create(problem=self.problem)
        pd.custom_checker_cpp = SimpleUploadedFile(
            "checker.cpp", b"int main() { return 0; }"
        )
        pd.save()
        h1 = compute_input_hash(self.problem)

        pd.custom_checker_cpp = SimpleUploadedFile(
            "checker.cpp", b"int main() { return 1; /* changed */ }"
        )
        pd.save()
        h2 = compute_input_hash(self.problem)
        self.assertNotEqual(h1, h2)

    def test_changes_when_solution_code_added(self):
        h1 = compute_input_hash(self.problem)
        ProblemSolutionCode.objects.create(
            problem=self.problem,
            order=0,
            source_code="print('hi')",
            language=self.language,
            expected_result="AC",
        )
        h2 = compute_input_hash(self.problem)
        self.assertNotEqual(h1, h2)

    def test_changes_when_solution_code_source_changes(self):
        sc = ProblemSolutionCode.objects.create(
            problem=self.problem,
            order=0,
            source_code="print('a')",
            language=self.language,
            expected_result="AC",
        )
        h1 = compute_input_hash(self.problem)
        sc.source_code = "print('b')"
        sc.save()
        h2 = compute_input_hash(self.problem)
        self.assertNotEqual(h1, h2)
