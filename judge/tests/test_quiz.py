"""
Quiz System Unit Tests

Tests for quiz grading, attempts, and workflows.
"""

import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import connection
from django.http import QueryDict
from django.template import engines
from django.test import RequestFactory, SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext, override

from jinja2 import nodes, TemplateSyntaxError
from markupsafe import Markup

from ai_features.quiz_import_service import (
    normalize_quiz_question_payload,
    parse_quiz_import_response,
)
from ai_features.quiz_ai_service import QuizAIService, has_question_text
from judge.models import (
    BestQuizAttempt,
    Course,
    CourseLesson,
    CourseLessonPrerequisite,
    CourseLessonProgress,
    CourseLessonQuiz,
    CourseRole,
    Language,
    Profile,
)
from judge.admin.quiz import QuizQuestionAdmin
from judge.models.course import RoleInCourse
from judge.models.quiz import (
    QuizQuestion,
    Quiz,
    QuizQuestionAssignment,
    QuizAttempt,
    QuizAnswer,
)
from judge.utils.quiz_grading import (
    validate_multiple_true_false_score_table,
    default_multiple_true_false_score_table,
    grade_multiple_choice,
    grade_multiple_answer,
    grade_multiple_true_false,
    grade_short_answer,
    grade_essay,
    grade_answer,
    auto_grade_quiz_attempt,
    calculate_attempt_score,
)
from judge.utils.quiz_attempts import (
    save_submitted_answers as _save_submitted_quiz_answers,
)
from judge.utils.course_prerequisites import get_lesson_lock_status
from judge.views.quiz import QuizQuestionForm


class TemplateTranslationRegressionTestCase(SimpleTestCase):
    def test_affected_translation_expressions_render_and_escape_values(self):
        engine = next(engine for engine in engines.all() if hasattr(engine, "env"))
        cases = [
            (
                "quiz/question_bank/detail.html",
                "%(count)s correct",
                {"loop": {"index0": 2}},
                {"count": 2},
            ),
            (
                "problem/contest_list_sidebar.html",
                "Show %(count)d more...",
                {"contest_list": list(range(7))},
                {"count": 2},
            ),
            (
                "organization/courses.html",
                "Courses in %(org)s",
                {"organization": {"name": "<script>School</script>"}},
                {"org": "<script>School</script>"},
            ),
            (
                "submission/status-testcases.html",
                "This problem's test data has an error: %(error)s",
                {"test_data_feedback": "<script>Feedback</script>"},
                {"error": "<script>Feedback</script>"},
            ),
        ]
        for path, message, context, values in cases:
            source = (Path(settings.BASE_DIR) / "templates" / path).read_text()
            expressions = [
                match.group()
                for match in re.finditer(r"{{[\s\S]*?}}", source)
                if message in match.group()
            ]
            self.assertEqual(len(expressions), 1)
            for language in ["en", "vi"]:
                with self.subTest(template=path, language=language), override(language):
                    rendered = engine.env.from_string(expressions[0]).render(**context)
                    self.assertEqual(rendered, str(Markup(gettext(message)) % values))
                    self.assertNotIn("<script>", rendered)

    def test_named_translation_placeholders_are_passed_to_jinja(self):
        """Jinja's new-style gettext formats inside the call, not afterward."""
        engine = next(engine for engine in engines.all() if hasattr(engine, "env"))
        self.assertTrue(engine.env.newstyle_gettext)
        missing = []
        for path in sorted((Path(settings.BASE_DIR) / "templates").rglob("*")):
            if path.suffix not in {".html", ".txt"}:
                continue
            source = path.read_text()
            try:
                trees = [engine.env.parse(source)]
            except TemplateSyntaxError:
                # Some legacy/Django templates cannot be parsed as a whole by
                # Jinja. Still check any Jinja-compatible output expressions.
                trees = []
                for match in re.finditer(r"{{[\s\S]*?}}", source):
                    try:
                        tree = engine.env.parse(match.group())
                    except TemplateSyntaxError:
                        continue
                    tree.set_lineno(source.count("\n", 0, match.start()) + 1)
                    trees.append(tree)
            for tree in trees:
                for call in tree.find_all(nodes.Call):
                    if not isinstance(call.node, nodes.Name) or call.node.name not in {
                        "_",
                        "gettext",
                        "ngettext",
                        "pgettext",
                        "npgettext",
                    }:
                        continue
                    required = set()
                    for arg in call.args:
                        if isinstance(arg, nodes.Const) and isinstance(arg.value, str):
                            required.update(
                                re.findall(r"(?<!%)%\(([^)]+)\)", arg.value)
                            )
                    supplied = {keyword.key for keyword in call.kwargs}
                    if call.node.name in {"ngettext", "npgettext"}:
                        supplied.add("num")
                    if required - supplied and call.dyn_kwargs is None:
                        missing.append(
                            f"{path.relative_to(settings.BASE_DIR)}:{call.lineno}: "
                            f"{sorted(required - supplied)}"
                        )
        self.assertEqual(
            missing, [], "Missing gettext keyword arguments:\n" + "\n".join(missing)
        )


class QuizQuestionTestCase(TestCase):
    """Tests for QuizQuestion model"""

    fixtures = ["language_small"]

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", email="test@test.com", password="testpass"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user,
            defaults={"language": Language.objects.first()},
        )

    def test_create_multiple_choice_question(self):
        """Test creating a multiple choice question"""
        question = QuizQuestion.objects.create(
            question_type="MC",
            title="Test MC Question",
            content="What is 2+2?",
            choices=[
                {"id": "a", "text": "3"},
                {"id": "b", "text": "4"},
                {"id": "c", "text": "5"},
            ],
            correct_answers={"answers": "b"},
        )

        self.assertEqual(question.question_type, "MC")
        self.assertEqual(len(question.choices), 3)
        self.assertEqual(question.correct_answers["answers"], "b")

    def test_create_multiple_answer_question(self):
        """Test creating a multiple answer question"""
        question = QuizQuestion.objects.create(
            question_type="MA",
            title="Test MA Question",
            content="Select all prime numbers",
            choices=[
                {"id": "a", "text": "2"},
                {"id": "b", "text": "3"},
                {"id": "c", "text": "4"},
                {"id": "d", "text": "5"},
            ],
            correct_answers={"answers": ["a", "b", "d"]},
        )

        self.assertEqual(question.question_type, "MA")
        self.assertIn("a", question.correct_answers["answers"])
        self.assertIn("b", question.correct_answers["answers"])
        self.assertIn("d", question.correct_answers["answers"])
        self.assertNotIn("c", question.correct_answers["answers"])

    def test_create_short_answer_question(self):
        """Test creating a short answer question"""
        question = QuizQuestion.objects.create(
            question_type="SA",
            title="Test SA Question",
            content="What is 2+3?",
            correct_answers={
                "type": "exact",
                "answers": ["5", "five"],
                "case_sensitive": False,
            },
        )

        self.assertEqual(question.question_type, "SA")
        self.assertEqual(question.correct_answers["type"], "exact")
        self.assertIn("5", question.correct_answers["answers"])

    def test_create_essay_question(self):
        """Test creating an essay question"""
        question = QuizQuestion.objects.create(
            question_type="ES",
            title="Test Essay Question",
            content="Explain the concept of recursion.",
            correct_answers=None,
        )

        self.assertEqual(question.question_type, "ES")
        self.assertIsNone(question.correct_answers)

    def test_create_true_false_question(self):
        """Test creating a true/false question"""
        question = QuizQuestion.objects.create(
            question_type="TF",
            title="Test TF Question",
            content="",
            choices=[{"id": "A", "text": "The sky is blue."}],
            correct_answers={"answers": {"A": True}},
        )

        self.assertEqual(question.question_type, "TF")
        self.assertEqual(question.correct_answers["answers"], {"A": True})


class QuizQuestionDetailTestCase(TestCase):
    """Tests for question bank detail rendering."""

    fixtures = ["language_small"]

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="admin", email="admin@test.com", password="testpass"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user,
            defaults={"language": Language.objects.first()},
        )

    def test_short_answer_with_null_answers_renders(self):
        question = QuizQuestion.objects.create(
            question_type="SA",
            title="Imported SA Question",
            content="What is 2+3?",
            choices=[],
            correct_answers={"answers": None},
        )

        self.client.force_login(self.user)
        response = self.client.get(f"/quiz/questions/{question.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Imported SA Question")
        self.assertContains(response, f"ID: {question.pk}")
        self.assertContains(response, f'data-question-id="{question.pk}"')
        self.assertNotContains(response, "Accepted Answers")

    def test_authoring_offers_one_true_false_type(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("question_bank_create"))
        self.assertContains(response, 'value="TF"')
        self.assertNotContains(response, 'value="MT"')
        self.assertNotContains(response, "legacyTrueFalse")

    def test_edit_uses_statement_editor_for_single_statement(self):
        self.client.force_login(self.user)
        question = QuizQuestion.objects.create(
            question_type="TF",
            title="True/False example",
            content="",
            choices=[{"id": "A", "text": "Statement text"}],
            correct_answers={"answers": {"A": False}},
        )
        response = self.client.get(reverse("question_bank_edit", args=[question.pk]))
        self.assertNotContains(response, "legacyTrueFalse")
        self.assertContains(response, "MultipleTrueFalseEditor")


class QuizQuestionBankDiscoverabilityTestCase(TestCase):
    """Tests for finding and identifying reusable quiz questions."""

    fixtures = ["language_small"]

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="questionadmin", email="questionadmin@test.com", password="pw"
        )
        Profile.objects.get_or_create(
            user=self.user,
            defaults={"language": Language.objects.first()},
        )
        self.client.force_login(self.user)

    def _create_question(self, title, question_type="MC", is_public=False):
        return QuizQuestion.objects.create(
            question_type=question_type,
            title=title,
            content="Question content without numeric identifiers",
            choices=[{"id": "a", "text": "Answer"}],
            correct_answers={"answers": "a"},
            is_public=is_public,
        )

    def test_select2_question_search_matches_exact_id(self):
        question = self._create_question("Findable only by primary key")

        response = self.client.get(
            reverse("quiz_question_select2"), {"term": f"Q{question.pk}"}
        )

        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual([item["id"] for item in results], [question.pk])
        self.assertEqual(results[0]["text"], f"Q{question.pk}: {question.title}")
        self.assertEqual(results[0]["type_code"], question.question_type)

    def test_search_finds_statement_text_without_shared_content(self):
        question = QuizQuestion.objects.create(
            question_type="TF",
            title="A neutral title",
            content="",
            choices=[{"id": "A", "text": "UniqueStatementKeyword"}],
            correct_answers={"answers": {"A": False}},
        )
        response = self.client.get(
            reverse("question_bank_list"), {"search": "UniqueStatementKeyword"}
        )
        self.assertEqual(
            [item.pk for item in response.context["questions"]], [question.pk]
        )
        response = self.client.get(
            reverse("quiz_question_select2"), {"term": "UniqueStatementKeyword"}
        )
        self.assertEqual(
            [item["id"] for item in response.json()["results"]], [question.pk]
        )

    def test_question_bank_search_matches_exact_id(self):
        target = self._create_question("Target question")
        other = self._create_question("Other question")

        response = self.client.get(
            reverse("question_bank_list"), {"search": f"#{target.pk}"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Target question")
        self.assertContains(response, f">{target.pk}</a>")
        self.assertNotContains(response, "Other question")
        self.assertNotContains(response, f">{other.pk}</a>")

    def test_statement_search_decodes_unicode_and_escapes_wildcards(self):
        question = QuizQuestion.objects.create(
            question_type="TF",
            title="Neutral",
            content="",
            choices=[
                {
                    "id": "OnlyAnIdentifier",
                    "text": 'Độ PHỨC tạp, 100% đúng, a_b, hi! and "quoted".',
                }
            ],
            correct_answers={"answers": {"OnlyAnIdentifier": True}},
        )
        for term in ("phức", "PHỨC", "ĐỘ", "100%", "a_b", "hi!", '"quoted"'):
            with self.subTest(term=term):
                response = self.client.get(
                    reverse("question_bank_list"), {"search": term}
                )
                self.assertEqual(
                    [item.pk for item in response.context["questions"]], [question.pk]
                )
                response = self.client.get(
                    reverse("quiz_question_select2"), {"term": term}
                )
                self.assertEqual(
                    [item["id"] for item in response.json()["results"]], [question.pk]
                )
        question.choices = [{"id": "OnlyAnIdentifier", "text": "100X đúng, axb, hiX"}]
        question.save(update_fields=["choices"])
        for term in ("100%", "a_b", "hi!", "OnlyAnIdentifier"):
            with self.subTest(no_match=term):
                response = self.client.get(
                    reverse("question_bank_list"), {"search": term}
                )
                self.assertEqual(list(response.context["questions"]), [])
                response = self.client.get(
                    reverse("quiz_question_select2"), {"term": term}
                )
                self.assertEqual(response.json()["results"], [])

    def test_statement_search_does_not_expose_private_questions(self):
        self.user.is_superuser = False
        self.user.save(update_fields=["is_superuser"])
        QuizQuestion.objects.create(
            question_type="TF",
            title="Private",
            content="",
            is_public=False,
            choices=[{"id": "A", "text": "Độ phức tạp"}],
            correct_answers={"answers": {"A": True}},
        )
        response = self.client.get(reverse("question_bank_list"), {"search": "phức"})
        self.assertEqual(list(response.context["questions"]), [])
        response = self.client.get(reverse("quiz_question_select2"), {"term": "phức"})
        self.assertEqual(response.json()["results"], [])

    def test_question_bank_id_column_sorts(self):
        first = self._create_question("First by ID")
        second = self._create_question("Second by ID")

        response = self.client.get(reverse("question_bank_list"), {"order": "id"})

        self.assertEqual(response.status_code, 200)
        ids = [question.pk for question in response.context["questions"]]
        self.assertEqual(ids, [first.pk, second.pk])
        self.assertContains(response, reverse("question_bank_list") + "?order=-id")

    def test_question_bank_requested_columns_sort(self):
        beta_public = self._create_question("Beta", question_type="SA", is_public=True)
        alpha_private = self._create_question(
            "Alpha", question_type="MC", is_public=False
        )

        response = self.client.get(reverse("question_bank_list"), {"order": "title"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [question.pk for question in response.context["questions"]],
            [alpha_private.pk, beta_public.pk],
        )

        response = self.client.get(
            reverse("question_bank_list"), {"order": "question_type"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [question.pk for question in response.context["questions"]],
            [alpha_private.pk, beta_public.pk],
        )

        response = self.client.get(
            reverse("question_bank_list"), {"order": "-is_public"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [question.pk for question in response.context["questions"]],
            [beta_public.pk, alpha_private.pk],
        )


class QuizImportParsingTestCase(TestCase):
    """Tests for AI quiz import payload normalization."""

    def test_short_answer_null_answers_normalizes_to_no_correct_answers(self):
        result = parse_quiz_import_response("""
            {
              "questions": [
                {
                  "title": "SA",
                  "question_type": "SA",
                  "content": "What is 2+3?",
                  "choices": [],
                  "correct_answers": {"answers": null}
                }
              ]
            }
            """)

        self.assertTrue(result["success"])
        question = result["questions"][0]
        self.assertEqual(question["choices"], [])
        self.assertIsNone(question["correct_answers"])

    def test_short_answer_text_answers_preserve_case(self):
        result = parse_quiz_import_response("""
            {
              "questions": [
                {
                  "title": "SA",
                  "question_type": "SA",
                  "content": "Spell pi",
                  "choices": [],
                  "correct_answers": {"answers": ["pi", "Pi"]}
                }
              ]
            }
            """)

        self.assertTrue(result["success"])
        question = result["questions"][0]
        self.assertEqual(question["correct_answers"]["answers"], ["pi", "Pi"])

    def test_short_answer_empty_answer_list_normalizes_to_no_correct_answers(self):
        result = parse_quiz_import_response("""
            {
              "questions": [
                {
                  "title": "SA",
                  "question_type": "SA",
                  "content": "What is unknown?",
                  "choices": [],
                  "correct_answers": {"answers": []}
                }
              ]
            }
            """)

        self.assertTrue(result["success"])
        question = result["questions"][0]
        self.assertIsNone(question["correct_answers"])
        self.assertEqual(result["summary"]["has_answers"], 0)

    def test_choice_ids_and_answers_are_normalized(self):
        choices, correct_answers = normalize_quiz_question_payload(
            "MC",
            [{"id": " a ", "text": "Choice A"}],
            {"answers": " a "},
        )

        self.assertEqual(choices, [{"id": "A", "text": "Choice A"}])
        self.assertEqual(correct_answers, {"answers": "A"})


class QuizTestCase(TestCase):
    """Tests for Quiz model"""

    fixtures = ["language_small"]

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", email="test@test.com", password="testpass"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user,
            defaults={"language": Language.objects.first()},
        )

        # Create questions
        self.mc_question = QuizQuestion.objects.create(
            question_type="MC",
            title="MC Question",
            content="What is 2+2?",
            choices=[
                {"id": "a", "text": "3"},
                {"id": "b", "text": "4"},
                {"id": "c", "text": "5"},
            ],
            correct_answers={"answers": "b"},
        )

        self.sa_question = QuizQuestion.objects.create(
            question_type="SA",
            title="SA Question",
            content="What is 2+3?",
            correct_answers={
                "type": "exact",
                "answers": ["5"],
                "case_sensitive": False,
            },
        )

    def test_create_quiz(self):
        """Test creating a quiz"""
        quiz = Quiz.objects.create(
            code="testquiz1",
            title="Test Quiz",
            description="A test quiz",
            time_limit=30,
        )

        self.assertEqual(quiz.code, "testquiz1")
        self.assertEqual(quiz.time_limit, 30)

    def test_add_questions_to_quiz(self):
        """Test adding questions to a quiz"""
        quiz = Quiz.objects.create(
            code="testquiz2",
            title="Test Quiz 2",
        )

        QuizQuestionAssignment.objects.create(
            quiz=quiz, question=self.mc_question, points=5, order=1
        )
        QuizQuestionAssignment.objects.create(
            quiz=quiz, question=self.sa_question, points=10, order=2
        )

        self.assertEqual(quiz.get_question_count(), 2)
        self.assertEqual(quiz.get_total_points(), 15)

    def test_quiz_questions_ordering(self):
        """Test that quiz questions are ordered correctly"""
        quiz = Quiz.objects.create(code="testquiz3", title="Test Quiz 3")

        QuizQuestionAssignment.objects.create(
            quiz=quiz, question=self.sa_question, points=10, order=2
        )
        QuizQuestionAssignment.objects.create(
            quiz=quiz, question=self.mc_question, points=5, order=1
        )

        questions = list(quiz.get_questions())
        self.assertEqual(questions[0].question, self.mc_question)
        self.assertEqual(questions[1].question, self.sa_question)


class QuizGradingTestCase(TestCase):
    """Tests for quiz grading utilities"""

    fixtures = ["language_small"]

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", email="test@test.com", password="testpass"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user,
            defaults={"language": Language.objects.first()},
        )

        # Create MC question
        self.mc_question = QuizQuestion.objects.create(
            question_type="MC",
            title="MC Question",
            content="What is 2+2?",
            choices=[
                {"id": "a", "text": "3"},
                {"id": "b", "text": "4"},
                {"id": "c", "text": "5"},
            ],
            correct_answers={"answers": "b"},
        )

        # Create MA question
        self.ma_question = QuizQuestion.objects.create(
            question_type="MA",
            title="MA Question",
            content="Select all prime numbers",
            choices=[
                {"id": "a", "text": "2"},
                {"id": "b", "text": "3"},
                {"id": "c", "text": "4"},
                {"id": "d", "text": "5"},
            ],
            correct_answers={"answers": ["a", "b", "d"]},
        )

        # Create SA question - exact match
        self.sa_question_exact = QuizQuestion.objects.create(
            question_type="SA",
            title="SA Question Exact",
            content="What is 2+3?",
            correct_answers={
                "type": "exact",
                "answers": ["5", "five"],
                "case_sensitive": False,
            },
        )

        # Create SA question - regex match
        self.sa_question_regex = QuizQuestion.objects.create(
            question_type="SA",
            title="SA Question Regex",
            content="Enter any number",
            correct_answers={
                "type": "regex",
                "answers": [r"^\d+$"],
                "case_sensitive": False,
            },
        )

        # Create Essay question
        self.essay_question = QuizQuestion.objects.create(
            question_type="ES",
            title="Essay Question",
            content="Explain recursion",
            correct_answers=None,
        )

        # Create TF question
        self.tf_question = QuizQuestion.objects.create(
            question_type="TF",
            title="TF Question",
            content="",
            choices=[{"id": "A", "text": "The sky is blue"}],
            correct_answers={"answers": {"A": True}},
        )

        # Create quiz with assignments
        self.quiz = Quiz.objects.create(code="gradingtest", title="Grading Test Quiz")

        QuizQuestionAssignment.objects.create(
            quiz=self.quiz, question=self.mc_question, points=5, order=1
        )
        QuizQuestionAssignment.objects.create(
            quiz=self.quiz, question=self.ma_question, points=10, order=2
        )
        QuizQuestionAssignment.objects.create(
            quiz=self.quiz, question=self.sa_question_exact, points=5, order=3
        )
        QuizQuestionAssignment.objects.create(
            quiz=self.quiz, question=self.tf_question, points=2, order=4
        )
        QuizQuestionAssignment.objects.create(
            quiz=self.quiz, question=self.essay_question, points=20, order=5
        )

        # Create attempt
        self.attempt = QuizAttempt.objects.create(
            user=self.profile, quiz=self.quiz, attempt_number=1
        )

    def test_multiple_choice_correct(self):
        """Test grading correct MC answer"""
        answer = QuizAnswer.objects.create(
            attempt=self.attempt, question=self.mc_question, answer="b"
        )

        points, is_correct = grade_multiple_choice(answer)

        self.assertEqual(points, 5)  # Points from assignment
        self.assertTrue(is_correct)

    def test_multiple_choice_incorrect(self):
        """Test grading incorrect MC answer"""
        answer = QuizAnswer.objects.create(
            attempt=self.attempt, question=self.mc_question, answer="a"
        )

        points, is_correct = grade_multiple_choice(answer)

        self.assertEqual(points, 0)
        self.assertFalse(is_correct)

    def test_multiple_answer_all_correct(self):
        """Test grading MA answer with all correct selections"""
        import json

        answer = QuizAnswer.objects.create(
            attempt=self.attempt,
            question=self.ma_question,
            answer=json.dumps(["a", "b", "d"]),
        )

        points, is_correct = grade_multiple_answer(answer)

        self.assertEqual(points, 10)
        self.assertTrue(is_correct)

    def test_multiple_answer_partial(self):
        """Test grading MA answer with partial selections (incorrect)"""
        import json

        answer = QuizAnswer.objects.create(
            attempt=self.attempt,
            question=self.ma_question,
            answer=json.dumps(["a", "b"]),  # Missing "d"
        )

        points, is_correct = grade_multiple_answer(answer)

        self.assertEqual(points, 0)  # All or nothing grading
        self.assertFalse(is_correct)

    def test_multiple_answer_with_wrong(self):
        """Test grading MA answer with wrong selection included"""
        import json

        answer = QuizAnswer.objects.create(
            attempt=self.attempt,
            question=self.ma_question,
            answer=json.dumps(["a", "b", "c", "d"]),  # "c" is wrong
        )

        points, is_correct = grade_multiple_answer(answer)

        self.assertEqual(points, 0)
        self.assertFalse(is_correct)

    def test_short_answer_exact_match(self):
        """Test grading SA with exact match"""
        answer = QuizAnswer.objects.create(
            attempt=self.attempt, question=self.sa_question_exact, answer="5"
        )

        points, is_correct, needs_manual = grade_short_answer(answer)

        self.assertEqual(points, 5)
        self.assertTrue(is_correct)
        self.assertFalse(needs_manual)

    def test_short_answer_case_insensitive(self):
        """Test grading SA with case insensitive match"""
        answer = QuizAnswer.objects.create(
            attempt=self.attempt, question=self.sa_question_exact, answer="Five"
        )

        points, is_correct, needs_manual = grade_short_answer(answer)

        self.assertEqual(points, 5)
        self.assertTrue(is_correct)
        self.assertFalse(needs_manual)

    def test_short_answer_incorrect(self):
        """Test grading SA with incorrect answer"""
        answer = QuizAnswer.objects.create(
            attempt=self.attempt, question=self.sa_question_exact, answer="6"
        )

        points, is_correct, needs_manual = grade_short_answer(answer)

        self.assertEqual(points, 0)
        self.assertFalse(is_correct)
        self.assertTrue(needs_manual)  # Non-empty wrong answer needs review

    def test_short_answer_regex_match(self):
        """Test grading SA with regex match"""
        answer = QuizAnswer.objects.create(
            attempt=self.attempt, question=self.sa_question_regex, answer="12345"
        )

        points, is_correct, needs_manual = grade_short_answer(answer)

        self.assertTrue(is_correct)
        self.assertFalse(needs_manual)

    def test_short_answer_regex_no_match(self):
        """Test grading SA with regex that doesn't match"""
        answer = QuizAnswer.objects.create(
            attempt=self.attempt, question=self.sa_question_regex, answer="abc"
        )

        points, is_correct, needs_manual = grade_short_answer(answer)

        self.assertEqual(points, 0)
        self.assertFalse(is_correct)
        self.assertTrue(needs_manual)

    def test_essay_needs_manual_grading(self):
        """Test that essay questions always need manual grading"""
        answer = QuizAnswer.objects.create(
            attempt=self.attempt,
            question=self.essay_question,
            answer="Recursion is when a function calls itself...",
        )

        points, is_correct, needs_manual = grade_essay(answer)

        self.assertEqual(points, 0)
        self.assertFalse(is_correct)
        self.assertTrue(needs_manual)

    def test_true_false_correct(self):
        """Test grading correct TF answer"""
        answer = QuizAnswer.objects.create(
            attempt=self.attempt,
            question=self.tf_question,
            answer=json.dumps({"A": True}),
        )

        points, is_correct, needs_manual = grade_answer(answer)

        self.assertEqual(points, 2)
        self.assertTrue(is_correct)

    def test_true_false_incorrect(self):
        """Test grading incorrect TF answer"""
        answer = QuizAnswer.objects.create(
            attempt=self.attempt,
            question=self.tf_question,
            answer=json.dumps({"A": False}),
        )

        points, is_correct, needs_manual = grade_answer(answer)

        self.assertEqual(points, 0)
        self.assertFalse(is_correct)

    def test_grade_answer_dispatcher(self):
        """Test grade_answer dispatches correctly"""
        mc_answer = QuizAnswer.objects.create(
            attempt=self.attempt, question=self.mc_question, answer="b"
        )
        points, is_correct, needs_manual = grade_answer(mc_answer)
        self.assertTrue(is_correct)
        self.assertFalse(needs_manual)

        essay_answer = QuizAnswer.objects.create(
            attempt=self.attempt, question=self.essay_question, answer="Essay text"
        )
        points, is_correct, needs_manual = grade_answer(essay_answer)
        self.assertTrue(needs_manual)


class MultipleAnswerGradingStrategyTestCase(TestCase):
    """Tests for Multiple Answer grading strategies"""

    fixtures = ["language_small"]

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", email="test@test.com", password="testpass"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user,
            defaults={"language": Language.objects.first()},
        )

        # Create MA question with 4 choices: A, B correct; C, D wrong
        self.ma_question = QuizQuestion.objects.create(
            question_type="MA",
            title="MA Grading Strategy Test",
            content="Select A and B",
            choices=[
                {"id": "a", "text": "Option A"},
                {"id": "b", "text": "Option B"},
                {"id": "c", "text": "Option C"},
                {"id": "d", "text": "Option D"},
            ],
            correct_answers={"answers": ["a", "b"]},
            grading_strategy="all_or_nothing",  # Default
        )

        # Create quiz
        self.quiz = Quiz.objects.create(code="mastrategytest", title="MA Strategy Test")
        QuizQuestionAssignment.objects.create(
            quiz=self.quiz, question=self.ma_question, points=10, order=1
        )

        # Create attempt
        self.attempt = QuizAttempt.objects.create(
            user=self.profile, quiz=self.quiz, attempt_number=1
        )

    def _grade_answer(self, selected_ids):
        """Helper to grade an answer with given selections"""
        import json

        answer = QuizAnswer.objects.create(
            attempt=self.attempt,
            question=self.ma_question,
            answer=json.dumps(selected_ids),
        )
        points, is_correct = grade_multiple_answer(answer)
        answer.delete()  # Clean up for next test
        return points, is_correct

    # ===== All or Nothing Strategy Tests =====

    def test_all_or_nothing_perfect(self):
        """All or Nothing: Perfect answer gets full points"""
        self.ma_question.grading_strategy = "all_or_nothing"
        self.ma_question.save()

        points, is_correct = self._grade_answer(["a", "b"])
        self.assertEqual(points, 10.0)
        self.assertTrue(is_correct)

    def test_all_or_nothing_partial_correct(self):
        """All or Nothing: Partial correct gets 0"""
        self.ma_question.grading_strategy = "all_or_nothing"
        self.ma_question.save()

        points, is_correct = self._grade_answer(["a"])
        self.assertEqual(points, 0.0)
        self.assertFalse(is_correct)

    def test_all_or_nothing_with_wrong(self):
        """All or Nothing: Correct + wrong gets 0"""
        self.ma_question.grading_strategy = "all_or_nothing"
        self.ma_question.save()

        points, is_correct = self._grade_answer(["a", "b", "c"])
        self.assertEqual(points, 0.0)
        self.assertFalse(is_correct)

    def test_all_or_nothing_only_wrong(self):
        """All or Nothing: Only wrong gets 0"""
        self.ma_question.grading_strategy = "all_or_nothing"
        self.ma_question.save()

        points, is_correct = self._grade_answer(["c", "d"])
        self.assertEqual(points, 0.0)
        self.assertFalse(is_correct)

    def test_all_or_nothing_empty(self):
        """All or Nothing: Empty answer gets 0"""
        self.ma_question.grading_strategy = "all_or_nothing"
        self.ma_question.save()

        points, is_correct = self._grade_answer([])
        self.assertEqual(points, 0.0)
        self.assertFalse(is_correct)

    # ===== Partial Credit Strategy Tests =====

    def test_partial_credit_perfect(self):
        """Partial Credit: Perfect answer gets full points"""
        self.ma_question.grading_strategy = "partial_credit"
        self.ma_question.save()

        points, is_correct = self._grade_answer(["a", "b"])
        self.assertEqual(points, 10.0)
        self.assertTrue(is_correct)

    def test_partial_credit_one_correct(self):
        """Partial Credit: One correct (of 2) gets 50%"""
        self.ma_question.grading_strategy = "partial_credit"
        self.ma_question.save()

        points, is_correct = self._grade_answer(["a"])
        self.assertEqual(points, 5.0)  # 1/2 correct = 50%
        self.assertFalse(is_correct)

    def test_partial_credit_all_correct_plus_one_wrong(self):
        """Partial Credit: All correct + 1 wrong gets 50%"""
        self.ma_question.grading_strategy = "partial_credit"
        self.ma_question.save()

        # 2/2 correct - 1/2 wrong = 1.0 - 0.5 = 0.5
        points, is_correct = self._grade_answer(["a", "b", "c"])
        self.assertEqual(points, 5.0)  # (2/2) - (1/2) = 0.5 * 10 = 5
        self.assertFalse(is_correct)

    def test_partial_credit_one_correct_one_wrong(self):
        """Partial Credit: 1 correct + 1 wrong gets 0%"""
        self.ma_question.grading_strategy = "partial_credit"
        self.ma_question.save()

        # 1/2 correct - 1/2 wrong = 0
        points, is_correct = self._grade_answer(["a", "c"])
        self.assertEqual(points, 0.0)
        self.assertFalse(is_correct)

    def test_partial_credit_only_wrong(self):
        """Partial Credit: Only wrong gets 0 (clamped)"""
        self.ma_question.grading_strategy = "partial_credit"
        self.ma_question.save()

        # 0/2 correct - 2/2 wrong = -1, clamped to 0
        points, is_correct = self._grade_answer(["c", "d"])
        self.assertEqual(points, 0.0)
        self.assertFalse(is_correct)

    def test_partial_credit_empty(self):
        """Partial Credit: Empty answer gets 0"""
        self.ma_question.grading_strategy = "partial_credit"
        self.ma_question.save()

        points, is_correct = self._grade_answer([])
        self.assertEqual(points, 0.0)
        self.assertFalse(is_correct)

    # ===== Right Minus Wrong Strategy Tests =====

    def test_right_minus_wrong_perfect(self):
        """Right Minus Wrong: Perfect answer gets full points"""
        self.ma_question.grading_strategy = "right_minus_wrong"
        self.ma_question.save()

        points, is_correct = self._grade_answer(["a", "b"])
        self.assertEqual(points, 10.0)
        self.assertTrue(is_correct)

    def test_right_minus_wrong_one_correct(self):
        """Right Minus Wrong: One correct gets 50%"""
        self.ma_question.grading_strategy = "right_minus_wrong"
        self.ma_question.save()

        # net = 1 - 0 = 1, ratio = 1/2 = 0.5
        points, is_correct = self._grade_answer(["a"])
        self.assertEqual(points, 5.0)
        self.assertFalse(is_correct)

    def test_right_minus_wrong_all_correct_plus_one_wrong(self):
        """Right Minus Wrong: All correct + 1 wrong gets 50%"""
        self.ma_question.grading_strategy = "right_minus_wrong"
        self.ma_question.save()

        # net = 2 - 1 = 1, ratio = 1/2 = 0.5
        points, is_correct = self._grade_answer(["a", "b", "c"])
        self.assertEqual(points, 5.0)
        self.assertFalse(is_correct)

    def test_right_minus_wrong_one_correct_one_wrong(self):
        """Right Minus Wrong: 1 correct + 1 wrong gets 0"""
        self.ma_question.grading_strategy = "right_minus_wrong"
        self.ma_question.save()

        # net = 1 - 1 = 0, ratio = 0/2 = 0
        points, is_correct = self._grade_answer(["a", "c"])
        self.assertEqual(points, 0.0)
        self.assertFalse(is_correct)

    def test_right_minus_wrong_only_wrong(self):
        """Right Minus Wrong: Only wrong gets 0 (clamped)"""
        self.ma_question.grading_strategy = "right_minus_wrong"
        self.ma_question.save()

        # net = 0 - 2 = -2, clamped to 0
        points, is_correct = self._grade_answer(["c", "d"])
        self.assertEqual(points, 0.0)
        self.assertFalse(is_correct)

    def test_right_minus_wrong_empty(self):
        """Right Minus Wrong: Empty answer gets 0"""
        self.ma_question.grading_strategy = "right_minus_wrong"
        self.ma_question.save()

        points, is_correct = self._grade_answer([])
        self.assertEqual(points, 0.0)
        self.assertFalse(is_correct)

    # ===== Correct Only Strategy Tests =====

    def test_correct_only_perfect(self):
        """Correct Only: Perfect answer gets full points"""
        self.ma_question.grading_strategy = "correct_only"
        self.ma_question.save()

        points, is_correct = self._grade_answer(["a", "b"])
        self.assertEqual(points, 10.0)
        self.assertTrue(is_correct)

    def test_correct_only_one_correct(self):
        """Correct Only: One correct gets 50%"""
        self.ma_question.grading_strategy = "correct_only"
        self.ma_question.save()

        points, is_correct = self._grade_answer(["a"])
        self.assertEqual(points, 5.0)  # 1/2 = 50%
        self.assertFalse(is_correct)

    def test_correct_only_all_correct_plus_wrong(self):
        """Correct Only: All correct + wrong still gets full points (no penalty)"""
        self.ma_question.grading_strategy = "correct_only"
        self.ma_question.save()

        # 2/2 correct = 100%, wrong answers don't penalize
        points, is_correct = self._grade_answer(["a", "b", "c"])
        self.assertEqual(points, 10.0)
        self.assertTrue(is_correct)

    def test_correct_only_one_correct_one_wrong(self):
        """Correct Only: 1 correct + 1 wrong gets 50% (no penalty)"""
        self.ma_question.grading_strategy = "correct_only"
        self.ma_question.save()

        # 1/2 correct = 50%, wrong doesn't penalize
        points, is_correct = self._grade_answer(["a", "c"])
        self.assertEqual(points, 5.0)
        self.assertFalse(is_correct)

    def test_correct_only_only_wrong(self):
        """Correct Only: Only wrong gets 0"""
        self.ma_question.grading_strategy = "correct_only"
        self.ma_question.save()

        # 0/2 correct = 0%
        points, is_correct = self._grade_answer(["c", "d"])
        self.assertEqual(points, 0.0)
        self.assertFalse(is_correct)

    def test_correct_only_empty(self):
        """Correct Only: Empty answer gets 0"""
        self.ma_question.grading_strategy = "correct_only"
        self.ma_question.save()

        points, is_correct = self._grade_answer([])
        self.assertEqual(points, 0.0)
        self.assertFalse(is_correct)

    # ===== Edge Case: All choices selected =====

    def test_all_strategies_all_selected(self):
        """Test all strategies when all choices are selected"""
        test_cases = [
            ("all_or_nothing", 0.0),  # Not exact match
            ("partial_credit", 0.0),  # 2/2 - 2/2 = 0
            ("right_minus_wrong", 0.0),  # 2 - 2 = 0
            ("correct_only", 10.0),  # 2/2 = 100%
        ]

        for strategy, expected_points in test_cases:
            self.ma_question.grading_strategy = strategy
            self.ma_question.save()
            points, _ = self._grade_answer(["a", "b", "c", "d"])
            self.assertEqual(
                points, expected_points, f"Failed for strategy: {strategy}"
            )


class MultipleTrueFalseGradingTestCase(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.user = User.objects.create_user(
            username="mtf_user", email="mtf@example.com", password="testpass"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user,
            defaults={"language": Language.objects.first()},
        )
        self.question = QuizQuestion.objects.create(
            question_type="TF",
            title="Grouped statements",
            content="Mark every statement.",
            choices=[
                {"id": "A", "text": "Statement A"},
                {"id": "B", "text": "Statement B"},
                {"id": "C", "text": "Statement C"},
                {"id": "D", "text": "Statement D"},
            ],
            correct_answers={"answers": {"A": True, "B": False, "C": True, "D": False}},
            multiple_true_false_score_table=[0, 10, 25, 50, 100],
        )
        self.quiz = Quiz.objects.create(
            code="mtfgrading", title="MTF grading", is_public=True
        )
        QuizQuestionAssignment.objects.create(
            quiz=self.quiz, question=self.question, points=20, order=1
        )
        self.attempt = QuizAttempt.objects.create(
            user=self.profile, quiz=self.quiz, attempt_number=1
        )

    def _grade(self, selected):
        answer, _ = QuizAnswer.objects.update_or_create(
            attempt=self.attempt,
            question=self.question,
            defaults={"answer": json.dumps(selected)},
        )
        return grade_multiple_true_false(answer)

    def test_detail_score_table_renders_in_english_and_vietnamese(self):
        self.question.authors.add(self.profile)
        self.client.force_login(self.user)
        for count in [1, 4, 5]:
            self.question.content = ""
            self.question.choices = [
                {"id": chr(65 + index), "text": f"Statement {index + 1}"}
                for index in range(count)
            ]
            self.question.correct_answers = {
                "answers": {choice["id"]: False for choice in self.question.choices}
            }
            self.question.multiple_true_false_score_table = (
                default_multiple_true_false_score_table(count)
            )
            self.question.save()
            for language in ["en", "vi"]:
                with self.subTest(count=count, language=language), override(language):
                    response = self.client.get(
                        reverse("question_bank_detail", args=[self.question.pk]),
                        HTTP_ACCEPT_LANGUAGE=language,
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["Content-Language"], language)
                    for correct_count, percentage in enumerate(
                        self.question.multiple_true_false_score_table
                    ):
                        label = gettext("%(count)s correct") % {"count": correct_count}
                        self.assertContains(
                            response, f"<li>{label}: {percentage}%</li>", html=True
                        )

    def test_default_score_table_tiers(self):
        expected_answers = {"A": True, "B": False, "C": True, "D": False}
        for correct_count, expected_points in enumerate([0, 2, 5, 10, 20]):
            selected = {
                statement_id: value if index < correct_count else not value
                for index, (statement_id, value) in enumerate(expected_answers.items())
            }
            points, is_correct, score_ratio, actual_correct_count = self._grade(
                selected
            )
            self.assertEqual(points, expected_points)
            self.assertEqual(actual_correct_count, correct_count)
            self.assertEqual(score_ratio, [0, 0.1, 0.25, 0.5, 1][correct_count])
            self.assertEqual(is_correct, correct_count == 4)

    def test_non_four_statement_default_is_proportional(self):
        self.assertEqual(default_multiple_true_false_score_table(3), [0, 33, 67, 100])
        self.assertEqual(
            default_multiple_true_false_score_table(4), [0, 10, 25, 50, 100]
        )
        self.assertEqual(
            default_multiple_true_false_score_table(8),
            [0, 13, 25, 38, 50, 63, 75, 88, 100],
        )

    def test_score_table_rejects_fractional_missing_and_nonmonotone_values(self):
        for table in (
            [0, 12.5, 100],
            [0, True, 100],
            [0, None, 100],
            [0, 101, 100],
            [1, 50, 100],
            [0, 50, 99],
            [0, 100],
        ):
            with self.subTest(table=table), self.assertRaises(ValidationError):
                validate_multiple_true_false_score_table(table, 2)
        with self.assertRaises(ValidationError):
            validate_multiple_true_false_score_table([0, 60, 50, 100], 3)

    def test_malformed_statement_ids_cannot_crash_or_inflate_grading(self):
        for choices in ([None], [{"id": ["A"]}], [{"id": "A"}, {"id": "A"}]):
            self.question.choices = choices
            self.question.save(update_fields=["choices"])
            self.assertEqual(self._grade({"A": True})[0], 0)

    def test_single_statement_tf_grades_false_and_unanswered_separately(self):
        self.question.choices = [{"id": "A", "text": "One statement"}]
        self.question.correct_answers = {"answers": {"A": False}}
        self.question.multiple_true_false_score_table = []
        self.question.save()
        for selected, expected in [({}, 0), ({"A": True}, 0), ({"A": False}, 20)]:
            self._grade(selected)
            answer = self.attempt.answers.get()
            self.assertEqual(grade_answer(answer)[0], expected)
            answer.auto_grade()
            answer.refresh_from_db()
            self.assertEqual(answer.points, expected)
            auto_grade_quiz_attempt(self.attempt)
            self.attempt.refresh_from_db()
            self.assertEqual(self.attempt.score, expected)

    def test_single_tf_stores_statement_submission_and_grading(self):
        self.question.choices = [{"id": "A", "text": "One statement"}]
        self.question.correct_answers = {"answers": {"A": False}}
        self.question.multiple_true_false_score_table = []
        self.question.save()
        post_data = QueryDict("", mutable=True)
        post_data[f"q_{self.question.id}"] = json.dumps({"A": False})
        answers = _save_submitted_quiz_answers(
            self.attempt, post_data, self.quiz.quiz_questions.select_related("question")
        )
        answer = answers[0]
        self.assertEqual(json.loads(answer.answer), {"A": False})
        self.assertEqual(answer.get_formatted_answer(), "A: " + gettext("False"))
        self.assertEqual(grade_answer(answer), (20, True, False))
        auto_grade_quiz_attempt(self.attempt)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.score, 20)
        self.question.refresh_from_db()
        self.assertEqual(self.question.correct_answers, {"answers": {"A": False}})

    def test_cannot_reinterpret_existing_tf_answers_as_other_types(self):
        self.question.authors.add(self.profile)
        self._grade({"A": True})
        form = QuizQuestionForm(
            instance=self.question,
            user=self.user,
            data={
                "title": self.question.title,
                "question_type": "TF",
                "content": self.question.content,
                "choices": json.dumps(self.question.choices),
                "correct_answers": json.dumps({"answers": "A"}),
                "grading_strategy": "all_or_nothing",
                "multiple_true_false_score_table": "[]",
                "authors": [self.profile.id],
            },
        )
        self.assertFalse(form.is_valid())
        for question_type in ("MC", "MA", "SA", "ES"):
            self.question.refresh_from_db()
            data = dict(form.data, question_type=question_type)
            changed_form = QuizQuestionForm(
                instance=self.question, user=self.user, data=data
            )
            self.assertFalse(changed_form.is_valid())
            self.assertEqual(
                changed_form.non_field_errors().as_data()[0].code,
                "tf_answer_format_change",
            )

    def test_unanswered_is_not_treated_as_false(self):
        points, is_correct, score_ratio, correct_count = self._grade({"A": True})
        self.assertEqual(correct_count, 1)
        self.assertEqual(points, 2)
        self.assertEqual(score_ratio, 0.1)
        self.assertFalse(is_correct)

    def _statement_edit_form(self, ids, admin_form=False):
        self.question.refresh_from_db()
        choices = [
            {"id": statement_id, "text": "Edited statement"} for statement_id in ids
        ]
        key = {
            statement_id: self.question.correct_answers["answers"].get(
                statement_id, True
            )
            for statement_id in ids
        }
        data = {
            "title": self.question.title,
            "question_type": "TF",
            "content": "",
            "choices": json.dumps(choices),
            "correct_answers": json.dumps({"answers": key}),
            "multiple_true_false_score_table": json.dumps(
                default_multiple_true_false_score_table(len(ids))
            ),
            "grading_strategy": "all_or_nothing",
            "authors": [self.profile.pk],
        }
        if admin_form:
            self.user.is_superuser = True
            request = RequestFactory().get("/")
            request.user = self.user
            form_class = QuizQuestionAdmin(QuizQuestion, admin.site).get_form(
                request, self.question
            )
            return form_class(data=data, instance=self.question)
        return QuizQuestionForm(data=data, instance=self.question, user=self.user)

    def test_used_statement_ids_cannot_be_renamed_or_removed(self):
        self.question.authors.add(self.profile)
        self._grade({"A": True, "B": False})
        before_answer = self.attempt.answers.values().get()
        before_question = (
            QuizQuestion.objects.filter(pk=self.question.pk).values().get()
        )
        for submitted in (False, True):
            self.attempt.is_submitted = submitted
            self.attempt.save(update_fields=["is_submitted"])
            for admin_form in (False, True):
                for ids in (["RENAMED", "B", "C", "D"], ["A", "B", "C"]):
                    with self.subTest(
                        submitted=submitted, admin_form=admin_form, ids=ids
                    ):
                        form = self._statement_edit_form(ids, admin_form=admin_form)
                        self.assertFalse(form.is_valid())
                        self.assertIn(
                            "tf_statement_ids_change",
                            [error.code for error in form.non_field_errors().as_data()],
                        )
        self.assertEqual(before_answer, self.attempt.answers.values().get())
        self.assertEqual(
            before_question,
            QuizQuestion.objects.filter(pk=self.question.pk).values().get(),
        )

    def test_unused_statement_ids_can_be_renamed_or_removed(self):
        self.question.authors.add(self.profile)
        for admin_form in (False, True):
            for ids in (["RENAMED", "B", "C", "D"], ["A", "B", "C"]):
                with self.subTest(admin_form=admin_form, ids=ids):
                    form = self._statement_edit_form(ids, admin_form=admin_form)
                    self.assertTrue(form.is_valid(), form.errors)

    def test_used_statements_can_be_reordered_and_text_edited(self):
        self.question.authors.add(self.profile)
        self._grade({"A": True, "B": False})
        for admin_form in (False, True):
            form = self._statement_edit_form(
                ["D", "C", "B", "A"], admin_form=admin_form
            )
            self.assertTrue(form.is_valid(), form.errors)
            form.save()
        self.question.refresh_from_db()
        self.assertEqual(
            grade_multiple_true_false(
                self.attempt.answers.select_related("question").get()
            )[0],
            5,
        )

    def test_unknown_and_non_boolean_answers_do_not_count(self):
        points, is_correct, score_ratio, correct_count = self._grade(
            {"A": "true", "B": 0, "unknown": True}
        )
        self.assertEqual(correct_count, 0)
        self.assertEqual(points, 0)
        self.assertEqual(score_ratio, 0)
        self.assertFalse(is_correct)

    def test_custom_score_table(self):
        self.question.multiple_true_false_score_table = [0, 20, 40, 70, 100]
        self.question.save()
        points, is_correct, score_ratio, correct_count = self._grade(
            {"A": True, "B": False, "C": True}
        )
        self.assertEqual(correct_count, 3)
        self.assertEqual(points, 14)
        self.assertEqual(score_ratio, 0.7)
        self.assertFalse(is_correct)

    def test_auto_grade_stores_actual_partial_credit(self):
        answer = QuizAnswer.objects.create(
            attempt=self.attempt,
            question=self.question,
            answer=json.dumps({"A": True, "B": False}),
        )
        auto_grade_quiz_attempt(self.attempt)
        answer.refresh_from_db()
        self.assertEqual(answer.points, 5)
        self.assertEqual(answer.partial_credit, Decimal("0.25"))
        self.assertFalse(answer.is_correct)

    def test_partial_credit_updates_lesson_grade_and_unlocks_prerequisite(self):
        course = Course.objects.create(
            name="MTF course",
            slug="mtf-course",
            about="Test",
            is_public=True,
            is_open=True,
        )
        CourseRole.objects.create(
            course=course, user=self.profile, role=RoleInCourse.STUDENT
        )
        lesson = CourseLesson.objects.create(
            course=course,
            title="MTF lesson",
            content="Test",
            order=1,
            points=100,
        )
        lesson_quiz = CourseLessonQuiz.objects.create(
            lesson=lesson, quiz=self.quiz, points=100
        )
        self.attempt.lesson_quiz = lesson_quiz
        self.attempt.is_submitted = True
        self.attempt.save(update_fields=["lesson_quiz", "is_submitted"])
        answer = QuizAnswer.objects.create(
            attempt=self.attempt,
            question=self.question,
            answer=json.dumps({"A": True, "B": False, "C": False, "D": True}),
        )

        next_lesson = CourseLesson.objects.create(
            course=course,
            title="Next lesson",
            content="Test",
            order=2,
            points=100,
        )
        CourseLessonPrerequisite.objects.create(
            course=course,
            source_order=lesson.order,
            target_order=next_lesson.order,
            required_percentage=25,
        )

        auto_grade_quiz_attempt(self.attempt)

        answer.refresh_from_db()
        self.attempt.refresh_from_db()
        best = BestQuizAttempt.objects.get(user=self.profile, lesson_quiz=lesson_quiz)
        progress = CourseLessonProgress.objects.get(user=self.profile, lesson=lesson)
        self.assertEqual(answer.points, Decimal("5.00"))
        self.assertEqual(answer.partial_credit, Decimal("0.25"))
        self.assertEqual(self.attempt.score, Decimal("5.00"))
        self.assertEqual(best.score, Decimal("5.00"))
        self.assertAlmostEqual(progress.percentage, 25)

        lock_status = get_lesson_lock_status(self.profile, course)
        self.assertFalse(lock_status[next_lesson.id])

    def test_question_form_validates_complete_key_and_table(self):
        base_data = {
            "title": "Valid grouped question",
            "question_type": "TF",
            "content": "",
            "choices": json.dumps(
                [
                    {"id": "A", "text": "One"},
                    {"id": "B", "text": "Two"},
                ]
            ),
            "correct_answers": json.dumps({"answers": {"A": True, "B": False}}),
            "grading_strategy": "all_or_nothing",
            "multiple_true_false_score_table": json.dumps([0, 25, 100]),
            "shuffle_choices": False,
            "tags": "",
            "is_public": False,
            "explanation": "",
        }
        form = QuizQuestionForm(data=base_data, user=self.user)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.content, "")
        for question_type in ("MC", "MA", "SA", "ES"):
            with self.subTest(question_type=question_type):
                other = QuizQuestionForm(
                    data=dict(base_data, question_type=question_type), user=self.user
                )
                self.assertFalse(other.is_valid())
                self.assertIn("content", other.errors)

        invalid_data = base_data.copy()
        invalid_data["correct_answers"] = json.dumps({"answers": {"A": True}})
        invalid_data["multiple_true_false_score_table"] = json.dumps([0, 50, 40])
        form = QuizQuestionForm(data=invalid_data, user=self.user)
        self.assertFalse(form.is_valid())
        self.assertTrue(form.non_field_errors())

    def test_admin_can_edit_migrated_tf_and_rejects_old_scalar_key(self):
        self.user.is_superuser = True
        request = RequestFactory().get("/")
        request.user = self.user
        form_class = QuizQuestionAdmin(QuizQuestion, admin.site).get_form(
            request, self.question
        )
        data = {
            "title": self.question.title,
            "question_type": "TF",
            "content": "",
            "choices": json.dumps(self.question.choices),
            "correct_answers": json.dumps(self.question.correct_answers),
            "multiple_true_false_score_table": json.dumps([0, 10, 25, 50, 100]),
            "authors": [self.profile.pk],
        }
        form = form_class(data=data, instance=self.question)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.fields["content"].required)
        form.save()
        self.question.refresh_from_db()
        self.assertEqual(self.question.content, "")
        form = form_class(
            data=dict(data, correct_answers=json.dumps({"answers": "A"})),
            instance=self.question,
        )
        self.assertFalse(form.is_valid())

    def test_submission_normalizes_mtf_answer(self):
        post_data = QueryDict("", mutable=True)
        post_data[f"q_{self.question.id}"] = json.dumps(
            {"A": True, "B": False, "unknown": True, "C": "true"}
        )
        answers = _save_submitted_quiz_answers(
            self.attempt,
            post_data,
            self.quiz.quiz_questions.select_related("question"),
        )
        self.assertEqual(
            json.loads(answers[0].answer),
            {"A": True, "B": False},
        )

    def test_take_page_restores_explicit_false_answer(self):
        QuizAnswer.objects.create(
            attempt=self.attempt,
            question=self.question,
            answer=json.dumps({"A": True, "B": False}),
        )
        self.client.force_login(self.user)
        response = self.client.get(
            reverse(
                "quiz_take",
                kwargs={"code": self.quiz.code, "attempt_id": self.attempt.id},
            )
        )
        self.assertContains(response, 'data-statement="A"')
        self.assertContains(response, 'data-statement="B"')
        self.assertContains(response, 'value="false"')

    def test_import_normalizes_multiple_true_false_answer_map(self):
        choices, correct_answers = normalize_quiz_question_payload(
            "TF",
            [
                {"id": "a", "text": "One"},
                {"id": "b", "text": "Two"},
            ],
            {"answers": {"a": True, "b": False}},
        )
        self.assertEqual([choice["id"] for choice in choices], ["A", "B"])
        self.assertEqual(correct_answers, {"answers": {"A": True, "B": False}})

    def test_import_accepts_statements_without_shared_content_or_title(self):
        result = parse_quiz_import_response(
            json.dumps(
                {
                    "questions": [
                        {
                            "question_type": "TF",
                            "content": "",
                            "choices": [{"id": "A", "text": "A statement"}],
                            "correct_answers": {"answers": {"A": False}},
                        }
                    ]
                }
            )
        )
        self.assertTrue(result["success"])
        self.assertEqual(len(result["questions"]), 1)
        self.assertTrue(result["questions"][0]["title"])
        self.assertEqual(result["questions"][0]["content"], "")

    def test_ai_markdown_supports_statements_without_shared_content(self):
        choices = json.dumps([{"id": "A", "text": "A statement"}])
        self.assertTrue(has_question_text("", choices))
        self.assertFalse(has_question_text("", "[]"))
        self.assertFalse(has_question_text("", "invalid"))
        service = QuizAIService.__new__(QuizAIService)
        service.llm_service = Mock()
        service.llm_service.call_llm.return_value = (
            'IMPROVED_CHOICES_JSON: ["**A statement**"]'
        )
        result = service.improve_question_markdown("", choices)
        self.assertTrue(result["success"])
        self.assertEqual(result["improved_markdown"], "")
        self.assertEqual(
            result["improved_choices"], [{"id": "A", "text": "**A statement**"}]
        )

    @patch("judge.views.quiz.can_use_ai_features", return_value=True)
    @patch("judge.views.quiz.improve_question_markdown_task.delay")
    @patch("judge.views.quiz.generate_question_explanation_task.delay")
    def test_ai_endpoints_dispatch_without_shared_content(
        self, explanation, markdown, _can_use_ai
    ):
        explanation.return_value.id = "explanation-task"
        markdown.return_value.id = "markdown-task"
        self.question.authors.add(self.profile)
        self.client.force_login(self.user)
        for action, task in [
            ("improve_question_markdown", markdown),
            ("generate_explanation", explanation),
        ]:
            with self.subTest(action=action):
                response = self.client.post(
                    reverse("question_bank_edit", args=[self.question.pk]),
                    {
                        action: "1",
                        "content": "",
                        "question_type": "TF",
                        "choices": json.dumps(self.question.choices),
                        "correct_answers": json.dumps(self.question.correct_answers),
                    },
                )
                self.assertTrue(response.json()["success"], response.content)
                task.assert_called_once()

    @patch("judge.views.quiz_import.can_use_ai_features", return_value=True)
    def test_import_endpoint_creates_mtf_with_default_score_table(self, _can_use_ai):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("quiz_import_create_question"),
            data=json.dumps(
                {
                    "title": "Imported statements",
                    "question_type": "TF",
                    "content": "",
                    "choices": [
                        {"id": "A", "text": "First"},
                        {"id": "B", "text": "Second"},
                        {"id": "C", "text": "Third"},
                        {"id": "D", "text": "Fourth"},
                    ],
                    "correct_answers": {
                        "answers": {"A": True, "B": False, "C": True, "D": False}
                    },
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        imported = QuizQuestion.objects.get(pk=response.json()["question_id"])
        self.assertEqual(imported.question_type, "TF")
        self.assertEqual(
            imported.multiple_true_false_score_table,
            [0, 10, 25, 50, 100],
        )
        self.assertEqual(imported.correct_answers["answers"]["B"], False)

    def test_result_page_shows_applied_score_tier(self):
        answer = QuizAnswer.objects.create(
            attempt=self.attempt,
            question=self.question,
            answer=json.dumps({"A": True, "B": False, "C": False, "D": True}),
        )
        self.attempt.is_submitted = True
        self.attempt.end_time = timezone.now()
        self.attempt.save(update_fields=["is_submitted", "end_time"])
        auto_grade_quiz_attempt(self.attempt)
        answer.refresh_from_db()

        self.quiz.is_shown_answer = True
        self.quiz.is_shown_correctness = True
        self.quiz.save(update_fields=["is_shown_answer", "is_shown_correctness"])
        self.client.force_login(self.user)
        response = self.client.get(
            reverse(
                "quiz_result",
                kwargs={"code": self.quiz.code, "attempt_id": self.attempt.id},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="mtf-score-summary"')
        self.assertContains(response, "25%")

    def _prepare_manual_tf_grade(self, table=None):
        self.quiz.authors.add(self.profile)
        self.quiz.is_shown_correctness = True
        self.quiz.save(update_fields=["is_shown_correctness"])
        QuizQuestionAssignment.objects.filter(quiz=self.quiz).update(points=1)
        if table:
            self.question.multiple_true_false_score_table = table
            self.question.save(update_fields=["multiple_true_false_score_table"])
        answer = QuizAnswer.objects.create(
            attempt=self.attempt,
            question=self.question,
            answer=json.dumps({"A": True, "B": False, "C": False, "D": True}),
        )
        self.attempt.is_submitted = True
        self.attempt.end_time = timezone.now()
        self.attempt.save(update_fields=["is_submitted", "end_time"])
        auto_grade_quiz_attempt(self.attempt)
        answer.refresh_from_db()
        self.client.force_login(self.user)
        return answer

    def test_manual_tf_feedback_preserves_exact_score_and_auto_grade(self):
        answer = self._prepare_manual_tf_grade()
        graded_at = answer.graded_at
        url = reverse("attempt_grade", args=[self.attempt.pk])
        response = self.client.get(url)
        rendered = re.search(
            rf'id="points_{answer.pk}"[\s\S]*?value="([^"]+)"',
            response.content.decode(),
        ).group(1)
        self.assertEqual(rendered, "0.25")
        response = self.client.post(
            url,
            {
                f"points_{answer.pk}": rendered,
                f"feedback_{answer.pk}": "Feedback only",
            },
        )
        self.assertEqual(response.status_code, 302)
        answer.refresh_from_db()
        self.assertEqual(answer.points, 0.25)
        self.assertEqual(answer.partial_credit, 0.25)
        self.assertFalse(answer.is_correct)
        self.assertEqual(answer.graded_at, graded_at)
        self.assertIsNone(answer.graded_by_id)
        self.assertEqual(answer.feedback, "Feedback only")

    def test_manual_tf_overrides_keep_score_ratio_and_correctness_consistent(self):
        answer = self._prepare_manual_tf_grade()
        for endpoint in ["attempt_grade", "answer_grade"]:
            for points in [0.57, 1.0, 0.0]:
                with self.subTest(endpoint=endpoint, points=points):
                    if endpoint == "attempt_grade":
                        response = self.client.post(
                            reverse(endpoint, args=[self.attempt.pk]),
                            {
                                f"points_{answer.pk}": str(points),
                                f"partial_{answer.pk}": "100",  # Cannot contradict TF points.
                            },
                        )
                        self.assertEqual(response.status_code, 302)
                    else:
                        response = self.client.post(
                            reverse(endpoint, args=[answer.pk]),
                            json.dumps({"points": points}),
                            content_type="application/json",
                        )
                        self.assertEqual(response.status_code, 200)
                    answer.refresh_from_db()
                    self.assertEqual(answer.points, points)
                    self.assertAlmostEqual(float(answer.partial_credit), points)
                    self.assertEqual(answer.is_correct, points == 1)
                    self.assertEqual(answer.graded_by_id, self.profile.pk)
                    self.attempt.refresh_from_db()
                    self.assertAlmostEqual(float(self.attempt.score), points)

    def test_unchanged_tf_full_credit_tier_keeps_statement_correctness(self):
        answer = self._prepare_manual_tf_grade([0, 100, 100, 100, 100])
        self.assertFalse(answer.is_correct)
        response = self.client.post(
            reverse("answer_grade", args=[answer.pk]),
            json.dumps({"points": 1}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        answer.refresh_from_db()
        self.assertFalse(answer.is_correct)
        self.assertIsNone(answer.graded_by_id)

    def test_custom_tf_percentage_summaries_round_instead_of_truncating(self):
        self._prepare_manual_tf_grade([0, 10, 29, 50, 100])
        for url in [
            reverse("attempt_grade", args=[self.attempt.pk]),
            reverse(
                "quiz_result",
                kwargs={"code": self.quiz.code, "attempt_id": self.attempt.pk},
            ),
        ]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "29%")
                self.assertNotContains(response, "28%")


class QuizAttemptTestCase(TestCase):
    """Tests for QuizAttempt model and related functionality"""

    fixtures = ["language_small"]

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", email="test@test.com", password="testpass"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user,
            defaults={"language": Language.objects.first()},
        )

        # Create questions
        self.mc_question = QuizQuestion.objects.create(
            question_type="MC",
            title="MC Question",
            content="What is 2+2?",
            choices=[
                {"id": "a", "text": "3"},
                {"id": "b", "text": "4"},
            ],
            correct_answers={"answers": "b"},
        )

        self.sa_question = QuizQuestion.objects.create(
            question_type="SA",
            title="SA Question",
            content="What is 2+3?",
            correct_answers={
                "type": "exact",
                "answers": ["5"],
                "case_sensitive": False,
            },
        )

        # Create quiz
        self.quiz = Quiz.objects.create(
            code="attempttest", title="Attempt Test Quiz", time_limit=30
        )

        QuizQuestionAssignment.objects.create(
            quiz=self.quiz, question=self.mc_question, points=5, order=1
        )
        QuizQuestionAssignment.objects.create(
            quiz=self.quiz, question=self.sa_question, points=10, order=2
        )

    def test_create_attempt(self):
        """Test creating a quiz attempt"""
        attempt = QuizAttempt.objects.create(
            user=self.profile,
            quiz=self.quiz,
            attempt_number=1,
            time_limit_minutes=self.quiz.time_limit,
        )

        self.assertEqual(attempt.attempt_number, 1)
        self.assertEqual(attempt.time_limit_minutes, 30)
        self.assertFalse(attempt.is_submitted)

    def test_attempt_duration(self):
        """Test attempt duration calculation"""
        attempt = QuizAttempt.objects.create(
            user=self.profile, quiz=self.quiz, attempt_number=1
        )
        attempt.end_time = attempt.start_time + timedelta(minutes=15)
        attempt.save()

        self.assertEqual(attempt.duration, timedelta(minutes=15))

    def test_attempt_time_limit_calculation(self):
        """Test time limit enforcement by checking deadline calculation"""
        attempt = QuizAttempt.objects.create(
            user=self.profile,
            quiz=self.quiz,
            attempt_number=1,
            time_limit_minutes=30,
        )

        # Check that deadline can be calculated
        if attempt.time_limit_minutes > 0:
            deadline = attempt.start_time + timedelta(
                minutes=attempt.time_limit_minutes
            )
            # Just created, deadline should be in the future
            self.assertGreater(deadline, timezone.now() - timedelta(seconds=5))

        # Modify start time to be in the past
        attempt.start_time = timezone.now() - timedelta(minutes=35)
        attempt.save()

        # Now deadline should be in the past
        deadline = attempt.start_time + timedelta(minutes=attempt.time_limit_minutes)
        self.assertLess(deadline, timezone.now())

    def test_auto_grade_quiz_attempt(self):
        """Test auto-grading a complete quiz attempt"""
        attempt = QuizAttempt.objects.create(
            user=self.profile, quiz=self.quiz, attempt_number=1
        )

        # Add correct answers
        QuizAnswer.objects.create(
            attempt=attempt, question=self.mc_question, answer="b"
        )
        QuizAnswer.objects.create(
            attempt=attempt, question=self.sa_question, answer="5"
        )

        # Grade the attempt
        total_score = auto_grade_quiz_attempt(attempt)

        attempt.refresh_from_db()
        self.assertEqual(float(attempt.score), 15.0)  # 5 + 10
        self.assertEqual(float(attempt.max_score), 15.0)

    def test_auto_grade_query_count_does_not_scale_per_question(self):
        attempt = QuizAttempt.objects.create(
            user=self.profile, quiz=self.quiz, attempt_number=1
        )
        extra_questions = []
        for index in range(20):
            extra_questions.append(
                QuizQuestion.objects.create(
                    question_type="MC",
                    title=f"Bulk question {index}",
                    content="Choose b",
                    choices=[
                        {"id": "a", "text": "Wrong"},
                        {"id": "b", "text": "Correct"},
                    ],
                    correct_answers={"answers": "b"},
                )
            )
        QuizQuestionAssignment.objects.bulk_create(
            [
                QuizQuestionAssignment(
                    quiz=self.quiz, question=question, points=1, order=index + 3
                )
                for index, question in enumerate(extra_questions)
            ]
        )
        QuizAnswer.objects.bulk_create(
            [
                QuizAnswer(attempt=attempt, question=question, answer="b")
                for question in [self.mc_question, self.sa_question, *extra_questions]
            ]
        )

        with CaptureQueriesContext(connection) as queries:
            auto_grade_quiz_attempt(attempt)

        self.assertLessEqual(len(queries), 8)
        self.assertEqual(QuizAnswer.objects.filter(attempt=attempt).count(), 22)

    def test_submission_answer_batch_preserves_ajax_answers(self):
        attempt = QuizAttempt.objects.create(
            user=self.profile, quiz=self.quiz, attempt_number=1
        )
        QuizAnswer.objects.create(
            attempt=attempt, question=self.mc_question, answer="b"
        )
        post_data = QueryDict("", mutable=True)
        post_data[f"q_{self.sa_question.id}"] = "5"
        post_data["q_999999"] = "invalid"
        assignments = list(
            self.quiz.quiz_questions.select_related("question").order_by("order")
        )

        with CaptureQueriesContext(connection) as queries:
            answers = _save_submitted_quiz_answers(attempt, post_data, assignments)

        # One extra batch UPDATE preserves the captured acceptance timestamp
        # after auto_now runs during INSERT; still constant in question count.
        self.assertLessEqual(len(queries), 4)
        self.assertEqual(
            {answer.question_id: answer.answer for answer in answers},
            {
                self.mc_question.id: "b",
                self.sa_question.id: "5",
            },
        )

    def test_submit_locks_attempt_and_grades_posted_answers(self):
        attempt = QuizAttempt.objects.create(
            user=self.profile, quiz=self.quiz, attempt_number=1
        )
        self.client.force_login(self.user)

        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(
                reverse(
                    "quiz_submit",
                    kwargs={"code": self.quiz.code, "attempt_id": attempt.id},
                ),
                {
                    f"q_{self.mc_question.id}": "b",
                    f"q_{self.sa_question.id}": "5",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(any("FOR UPDATE" in query["sql"].upper() for query in queries))
        attempt.refresh_from_db()
        self.assertTrue(attempt.is_submitted)
        self.assertEqual(float(attempt.score), 15.0)

    def test_auto_submit_query_count_does_not_scale_per_answer(self):
        attempt = QuizAttempt.objects.create(
            user=self.profile, quiz=self.quiz, attempt_number=1
        )
        extra_questions = QuizQuestion.objects.bulk_create(
            [
                QuizQuestion(
                    question_type="MC",
                    title=f"Auto-submit question {index}",
                    content="Choose b",
                    choices=[
                        {"id": "a", "text": "Wrong"},
                        {"id": "b", "text": "Correct"},
                    ],
                    correct_answers={"answers": "b"},
                )
                for index in range(20)
            ]
        )
        QuizQuestionAssignment.objects.bulk_create(
            [
                QuizQuestionAssignment(
                    quiz=self.quiz,
                    question=question,
                    points=1,
                    order=index + 3,
                )
                for index, question in enumerate(extra_questions)
            ]
        )
        QuizAnswer.objects.bulk_create(
            [
                QuizAnswer(
                    attempt=attempt,
                    question=question,
                    answer="5" if question == self.sa_question else "b",
                )
                for question in [
                    self.mc_question,
                    self.sa_question,
                    *extra_questions,
                ]
            ]
        )

        with CaptureQueriesContext(connection) as queries:
            attempt.auto_submit()

        self.assertLessEqual(len(queries), 10)
        attempt.refresh_from_db()
        self.assertTrue(attempt.is_submitted)
        self.assertEqual(float(attempt.score), 35.0)

    def test_calculate_attempt_score(self):
        """Test calculating attempt score"""
        attempt = QuizAttempt.objects.create(
            user=self.profile, quiz=self.quiz, attempt_number=1
        )

        # Add answers with points
        answer1 = QuizAnswer.objects.create(
            attempt=attempt, question=self.mc_question, answer="b"
        )
        answer1.points = 5
        answer1.save()

        answer2 = QuizAnswer.objects.create(
            attempt=attempt, question=self.sa_question, answer="5"
        )
        answer2.points = 10
        answer2.save()

        score, max_score = calculate_attempt_score(attempt)

        self.assertEqual(score, 15)
        self.assertEqual(max_score, 15)


class QuizIntegrationTestCase(TransactionTestCase):
    """Integration tests for complete quiz workflows"""

    fixtures = ["language_small"]

    def setUp(self):
        # Create teacher user
        self.teacher = User.objects.create_user(
            username="teacher", email="teacher@test.com", password="teacherpass"
        )
        self.teacher_profile, _ = Profile.objects.get_or_create(
            user=self.teacher,
            defaults={"language": Language.objects.first()},
        )

        # Create student user
        self.student = User.objects.create_user(
            username="student", email="student@test.com", password="studentpass"
        )
        self.student_profile, _ = Profile.objects.get_or_create(
            user=self.student,
            defaults={"language": Language.objects.first()},
        )

    def test_complete_quiz_workflow(self):
        """Test complete workflow: create quiz, student takes it, gets graded"""
        # Teacher creates questions
        q1 = QuizQuestion.objects.create(
            question_type="MC",
            title="Question 1",
            content="What is 1+1?",
            choices=[{"id": "a", "text": "1"}, {"id": "b", "text": "2"}],
            correct_answers={"answers": "b"},
        )
        q1.authors.add(self.teacher_profile)

        q2 = QuizQuestion.objects.create(
            question_type="SA",
            title="Question 2",
            content="What is 2+2?",
            correct_answers={
                "type": "exact",
                "answers": ["4"],
                "case_sensitive": False,
            },
        )
        q2.authors.add(self.teacher_profile)

        # Teacher creates quiz
        quiz = Quiz.objects.create(
            code="workflow1",
            title="Workflow Test Quiz",
            time_limit=60,
        )
        quiz.authors.add(self.teacher_profile)

        QuizQuestionAssignment.objects.create(quiz=quiz, question=q1, points=5, order=1)
        QuizQuestionAssignment.objects.create(
            quiz=quiz, question=q2, points=10, order=2
        )

        # Student starts attempt
        attempt = QuizAttempt.objects.create(
            user=self.student_profile,
            quiz=quiz,
            attempt_number=1,
            time_limit_minutes=quiz.time_limit,
        )

        # Student answers questions
        QuizAnswer.objects.create(attempt=attempt, question=q1, answer="b")  # Correct
        QuizAnswer.objects.create(attempt=attempt, question=q2, answer="4")  # Correct

        # Student submits
        attempt.is_submitted = True
        attempt.end_time = timezone.now()
        attempt.save()

        # Auto-grade
        auto_grade_quiz_attempt(attempt)

        # Verify results
        attempt.refresh_from_db()
        self.assertEqual(float(attempt.score), 15.0)
        self.assertEqual(float(attempt.max_score), 15.0)
        self.assertTrue(attempt.is_submitted)

    def test_partial_score_workflow(self):
        """Test workflow with partial scoring"""
        # Create questions
        q1 = QuizQuestion.objects.create(
            question_type="MC",
            title="Q1",
            content="What is 1+1?",
            choices=[{"id": "a", "text": "1"}, {"id": "b", "text": "2"}],
            correct_answers={"answers": "b"},
        )
        q2 = QuizQuestion.objects.create(
            question_type="MC",
            title="Q2",
            content="What is 2+2?",
            choices=[{"id": "a", "text": "3"}, {"id": "b", "text": "4"}],
            correct_answers={"answers": "b"},
        )

        # Create quiz
        quiz = Quiz.objects.create(code="partial1", title="Partial Test")
        QuizQuestionAssignment.objects.create(quiz=quiz, question=q1, points=5, order=1)
        QuizQuestionAssignment.objects.create(quiz=quiz, question=q2, points=5, order=2)

        # Student attempt - one correct, one wrong
        attempt = QuizAttempt.objects.create(
            user=self.student_profile, quiz=quiz, attempt_number=1
        )
        QuizAnswer.objects.create(attempt=attempt, question=q1, answer="b")  # Correct
        QuizAnswer.objects.create(attempt=attempt, question=q2, answer="a")  # Wrong

        attempt.is_submitted = True
        attempt.save()

        # Grade
        auto_grade_quiz_attempt(attempt)

        attempt.refresh_from_db()
        self.assertEqual(float(attempt.score), 5.0)  # Only Q1 correct
        self.assertEqual(float(attempt.max_score), 10.0)

    def test_essay_manual_grading_workflow(self):
        """Test workflow for essay questions requiring manual grading"""
        # Create essay question
        essay_q = QuizQuestion.objects.create(
            question_type="ES",
            title="Essay",
            content="Write about recursion",
        )

        # Create quiz
        quiz = Quiz.objects.create(code="essay1", title="Essay Test")
        QuizQuestionAssignment.objects.create(
            quiz=quiz, question=essay_q, points=20, order=1
        )

        # Student attempt
        attempt = QuizAttempt.objects.create(
            user=self.student_profile, quiz=quiz, attempt_number=1
        )
        essay_answer = QuizAnswer.objects.create(
            attempt=attempt,
            question=essay_q,
            answer="Recursion is a technique where a function calls itself...",
        )

        attempt.is_submitted = True
        attempt.save()

        # Auto-grade - essay should get 0 and need manual grading
        auto_grade_quiz_attempt(attempt)

        attempt.refresh_from_db()
        essay_answer.refresh_from_db()

        self.assertEqual(float(attempt.score), 0)  # Essay not graded yet
        self.assertIsNone(essay_answer.graded_at)  # Not graded

        # Teacher manually grades
        essay_answer.points = 15
        essay_answer.is_correct = True
        essay_answer.partial_credit = Decimal("0.75")
        essay_answer.feedback = "Good explanation, but missing some details."
        essay_answer.graded_at = timezone.now()
        essay_answer.save()

        # Recalculate score
        score, max_score = calculate_attempt_score(attempt)
        attempt.score = score
        attempt.max_score = max_score
        attempt.save()

        attempt.refresh_from_db()
        self.assertEqual(float(attempt.score), 15.0)


class QuizEdgeTestCase(TestCase):
    """Tests for edge cases"""

    fixtures = ["language_small"]

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", email="test@test.com", password="testpass"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user,
            defaults={"language": Language.objects.first()},
        )

    def test_empty_answer(self):
        """Test grading empty answers"""
        question = QuizQuestion.objects.create(
            question_type="MC",
            title="Test",
            content="Test?",
            choices=[{"id": "a", "text": "A"}],
            correct_answers={"answers": "a"},
        )
        quiz = Quiz.objects.create(code="edge1", title="Edge Test")
        QuizQuestionAssignment.objects.create(quiz=quiz, question=question, points=5)
        attempt = QuizAttempt.objects.create(user=self.profile, quiz=quiz)

        answer = QuizAnswer.objects.create(
            attempt=attempt, question=question, answer=""
        )

        points, is_correct = grade_multiple_choice(answer)

        self.assertEqual(points, 0)
        self.assertFalse(is_correct)

    def test_no_correct_answers_defined(self):
        """Test grading when question has no correct answers defined"""
        question = QuizQuestion.objects.create(
            question_type="MC",
            title="Test",
            content="Test?",
            choices=[{"id": "a", "text": "A"}],
            correct_answers=None,  # No correct answers
        )
        quiz = Quiz.objects.create(code="edge2", title="Edge Test 2")
        QuizQuestionAssignment.objects.create(quiz=quiz, question=question, points=5)
        attempt = QuizAttempt.objects.create(user=self.profile, quiz=quiz)

        answer = QuizAnswer.objects.create(
            attempt=attempt, question=question, answer="a"
        )

        points, is_correct = grade_multiple_choice(answer)

        self.assertEqual(points, 0)
        self.assertFalse(is_correct)

    def test_short_answer_empty_needs_no_manual(self):
        """Test that empty short answers don't need manual grading"""
        question = QuizQuestion.objects.create(
            question_type="SA",
            title="Test",
            content="Test?",
            correct_answers={"type": "exact", "answers": ["answer"]},
        )
        quiz = Quiz.objects.create(code="edge3", title="Edge Test 3")
        QuizQuestionAssignment.objects.create(quiz=quiz, question=question, points=5)
        attempt = QuizAttempt.objects.create(user=self.profile, quiz=quiz)

        answer = QuizAnswer.objects.create(
            attempt=attempt, question=question, answer=""
        )

        points, is_correct, needs_manual = grade_short_answer(answer)

        self.assertEqual(points, 0)
        self.assertFalse(is_correct)
        self.assertFalse(needs_manual)  # Empty = no review needed

    def test_multiple_attempts(self):
        """Test multiple attempts on same quiz"""
        question = QuizQuestion.objects.create(
            question_type="MC",
            title="Test",
            content="What is 1+1?",
            choices=[{"id": "a", "text": "2"}, {"id": "b", "text": "3"}],
            correct_answers={"answers": "a"},
        )
        quiz = Quiz.objects.create(code="edge4", title="Edge Test 4")
        QuizQuestionAssignment.objects.create(quiz=quiz, question=question, points=10)

        # First attempt - wrong
        attempt1 = QuizAttempt.objects.create(
            user=self.profile, quiz=quiz, attempt_number=1
        )
        QuizAnswer.objects.create(attempt=attempt1, question=question, answer="b")
        attempt1.is_submitted = True
        attempt1.save()
        auto_grade_quiz_attempt(attempt1)

        # Second attempt - correct
        attempt2 = QuizAttempt.objects.create(
            user=self.profile, quiz=quiz, attempt_number=2
        )
        QuizAnswer.objects.create(attempt=attempt2, question=question, answer="a")
        attempt2.is_submitted = True
        attempt2.save()
        auto_grade_quiz_attempt(attempt2)

        attempt1.refresh_from_db()
        attempt2.refresh_from_db()

        self.assertEqual(float(attempt1.score), 0)
        self.assertEqual(float(attempt2.score), 10)

        # Check best score
        best_score = quiz.get_best_score(self.profile)
        self.assertEqual(float(best_score), 10)
