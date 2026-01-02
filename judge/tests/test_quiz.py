"""
Quiz System Unit Tests

Tests for quiz grading, attempts, and workflows.
"""

from django.test import TestCase, TransactionTestCase
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

from judge.models import Profile
from judge.models.quiz import (
    QuizQuestion,
    Quiz,
    QuizQuestionAssignment,
    QuizAttempt,
    QuizAnswer,
    CourseLessonQuiz,
)
from judge.utils.quiz_grading import (
    grade_multiple_choice,
    grade_multiple_answer,
    grade_short_answer,
    grade_essay,
    grade_answer,
    auto_grade_answer,
    auto_grade_quiz_attempt,
    calculate_attempt_score,
)


class QuizQuestionTestCase(TestCase):
    """Tests for QuizQuestion model"""

    fixtures = ["language_small"]

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", email="test@test.com", password="testpass"
        )
        self.profile, _ = Profile.objects.get_or_create(user=self.user)

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
            content="The sky is blue.",
            choices=[
                {"id": "true", "text": "True"},
                {"id": "false", "text": "False"},
            ],
            correct_answers={"answers": "true"},
        )

        self.assertEqual(question.question_type, "TF")
        self.assertEqual(question.correct_answers["answers"], "true")


class QuizTestCase(TestCase):
    """Tests for Quiz model"""

    fixtures = ["language_small"]

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", email="test@test.com", password="testpass"
        )
        self.profile, _ = Profile.objects.get_or_create(user=self.user)

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
        self.profile, _ = Profile.objects.get_or_create(user=self.user)

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
            content="The sky is blue",
            choices=[
                {"id": "true", "text": "True"},
                {"id": "false", "text": "False"},
            ],
            correct_answers={"answers": "true"},
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
            attempt=self.attempt, question=self.tf_question, answer="true"
        )

        points, is_correct = grade_multiple_choice(answer)  # TF uses same grading as MC

        self.assertEqual(points, 2)
        self.assertTrue(is_correct)

    def test_true_false_incorrect(self):
        """Test grading incorrect TF answer"""
        answer = QuizAnswer.objects.create(
            attempt=self.attempt, question=self.tf_question, answer="false"
        )

        points, is_correct = grade_multiple_choice(answer)

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
        self.profile, _ = Profile.objects.get_or_create(user=self.user)

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


class QuizAttemptTestCase(TestCase):
    """Tests for QuizAttempt model and related functionality"""

    fixtures = ["language_small"]

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", email="test@test.com", password="testpass"
        )
        self.profile, _ = Profile.objects.get_or_create(user=self.user)

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
        self.teacher_profile, _ = Profile.objects.get_or_create(user=self.teacher)

        # Create student user
        self.student = User.objects.create_user(
            username="student", email="student@test.com", password="studentpass"
        )
        self.student_profile, _ = Profile.objects.get_or_create(user=self.student)

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
        self.profile, _ = Profile.objects.get_or_create(user=self.user)

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
