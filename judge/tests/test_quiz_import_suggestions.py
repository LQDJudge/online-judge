import json
from unittest.mock import Mock, patch

from django.test import RequestFactory, SimpleTestCase

from ai_features.quiz_import_service import parse_quiz_import_response
from judge.views.quiz_import import QuizImportCreateQuestionView


class QuizImportSuggestionTests(SimpleTestCase):
    @patch("judge.views.quiz_import.can_use_ai_features", return_value=True)
    @patch("judge.views.quiz_import.QuizQuestion.objects.create")
    def test_creation_does_not_implicitly_apply_suggestions(self, create, permission):
        create.return_value = Mock(pk=123)
        for question_type, answers in [
            ("MC", "A"),
            ("MA", ["A"]),
            ("SA", ["5"]),
            ("TF", {"A": False}),
        ]:
            with self.subTest(question_type=question_type):
                create.reset_mock()
                request = RequestFactory().post(
                    "/quiz/import/create-question/",
                    data=json.dumps(
                        {
                            "title": "Example",
                            "content": "Question",
                            "question_type": question_type,
                            "choices": [{"id": "A", "text": "Statement"}],
                            "correct_answers": None,
                            "suggested_answers": {"answers": answers},
                        }
                    ),
                    content_type="application/json",
                )
                request.user = Mock()
                request.profile = Mock()
                response = QuizImportCreateQuestionView.as_view()(request)
                if question_type == "TF":
                    self.assertEqual(response.status_code, 400)
                    create.assert_not_called()
                else:
                    self.assertEqual(response.status_code, 200)
                    self.assertIsNone(create.call_args.kwargs["correct_answers"])
                    self.assertNotIn("suggested_answers", create.call_args.kwargs)

    def parse(self, question_type, **overrides):
        payload = {
            "title": "Example",
            "content": "Question",
            "question_type": question_type,
            "choices": [{"id": "A", "text": "First"}, {"id": "B", "text": "Second"}],
            "correct_answers": None,
            **overrides,
        }
        return parse_quiz_import_response(json.dumps({"questions": [payload]}))

    def test_suggestions_remain_separate_for_all_objective_types(self):
        for question_type, answers in [
            ("MC", "B"),
            ("MA", ["A", "B"]),
            ("TF", {"A": True, "B": False}),
            ("SA", ["5", "five"]),
        ]:
            with self.subTest(question_type=question_type):
                result = self.parse(
                    question_type,
                    suggested_answers={"answers": answers},
                    suggestion_explanation=" A short explanation. ",
                )
                question = result["questions"][0]
                self.assertIsNone(question["correct_answers"])
                self.assertIsNone(question["answer_source"])
                self.assertEqual(question["suggested_answers"]["answers"], answers)
                self.assertEqual(
                    question["suggestion_explanation"], "A short explanation."
                )
                self.assertEqual(result["summary"]["has_answers"], 0)

    def test_document_answer_takes_precedence(self):
        question = self.parse(
            "MC",
            correct_answers={"answers": "A"},
            suggested_answers={"answers": "B"},
            suggestion_explanation="Conflicting guess",
        )["questions"][0]
        self.assertEqual(question["correct_answers"], {"answers": "A"})
        self.assertEqual(question["answer_source"], "document")
        self.assertIsNone(question["suggested_answers"])
        self.assertEqual(question["suggestion_explanation"], "")

    def test_essay_never_has_answers_or_suggestions(self):
        question = self.parse(
            "ES",
            correct_answers={"answers": ["Essay"]},
            suggested_answers={"answers": ["Essay"]},
            suggestion_explanation="Unwanted explanation",
        )["questions"][0]
        self.assertIsNone(question["correct_answers"])
        self.assertIsNone(question["suggested_answers"])
        self.assertEqual(question["suggestion_explanation"], "")

    def test_rejects_incomplete_invalid_and_unknown_choice_suggestions(self):
        for question_type, answers in [
            ("MC", "Z"),
            ("MA", ["A", "Z"]),
            ("MA", []),
            ("TF", {"A": True}),
            ("TF", {"A": True, "B": "false"}),
            ("TF", {"A": True, "B": False, "Z": False}),
            ("SA", []),
        ]:
            with self.subTest(question_type=question_type, answers=answers):
                question = self.parse(
                    question_type, suggested_answers={"answers": answers}
                )["questions"][0]
                self.assertIsNone(question["suggested_answers"])
                self.assertIsNone(question["correct_answers"])

    def test_old_results_and_uncertainty_are_safe(self):
        question = self.parse("TF")["questions"][0]
        self.assertIsNone(question["suggested_answers"])
        question = self.parse(
            "TF", suggestion_explanation="The diagram is unreadable."
        )["questions"][0]
        self.assertIsNone(question["suggested_answers"])
        self.assertEqual(
            question["suggestion_explanation"], "The diagram is unreadable."
        )
        question = self.parse("TF", suggestion_explanation={"unexpected": True})[
            "questions"
        ][0]
        self.assertEqual(question["suggestion_explanation"], "")

    def test_single_false_suggestion_is_not_missing(self):
        question = self.parse(
            "TF",
            choices=[{"id": " a ", "text": "Statement"}],
            suggested_answers={"answers": {" a ": False}},
        )["questions"][0]
        self.assertEqual(question["suggested_answers"], {"answers": {"A": False}})

    def test_short_answer_suggestions_use_exact_matching(self):
        question = self.parse(
            "SA",
            suggested_answers={
                "answers": ["5"],
                "type": "regex",
                "case_sensitive": True,
            },
        )["questions"][0]
        self.assertEqual(
            question["suggested_answers"],
            {
                "answers": ["5"],
                "type": "exact",
                "case_sensitive": False,
            },
        )
