"""Short-answer (SA) regex grading must match the WHOLE answer, not just a prefix.

Bug: grade_short_answer used re.match (anchors start only), so an answer that merely
*begins with* an accepted pattern was graded correct. e.g. accepted "Chloe: 5" would
accept "Chloe: 5 , Leo: 8 , Emma: 13 , Lily: 14" (wrong Lily) because it starts with
"Chloe: 5". Fix: re.fullmatch (entire answer must match a pattern).
"""

from django.contrib.auth.models import User
from django.test import TestCase

from judge.models import Language, Profile
from judge.models.quiz import (
    Quiz,
    QuizAnswer,
    QuizAttempt,
    QuizQuestion,
    QuizQuestionAssignment,
)
from judge.utils.quiz_grading import grade_short_answer


class ShortAnswerRegexGradingTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.lang = Language.objects.first()
        user = User.objects.create_user("sauser", "sa@x.com", "pw")
        self.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.lang}
        )
        self.quiz = Quiz.objects.create(code="saq", title="SAQ")

    def _grade(self, accepted, submitted, case_sensitive=False):
        question = QuizQuestion.objects.create(
            question_type="SA",
            title="q",
            content="c",
            correct_answers={
                "type": "regex",
                "case_sensitive": case_sensitive,
                "answers": accepted,
            },
        )
        QuizQuestionAssignment.objects.create(
            quiz=self.quiz, question=question, points=10, order=1
        )
        attempt = QuizAttempt.objects.create(
            user=self.profile, quiz=self.quiz, attempt_number=1, is_submitted=True
        )
        answer = QuizAnswer.objects.create(
            attempt=attempt, question=question, answer=submitted
        )
        points, is_correct, _needs_manual = grade_short_answer(answer)
        return is_correct

    # --- the reported scenario --------------------------------------------------
    def test_combined_answer_with_one_wrong_part_is_rejected(self):
        accepted = ["Chloe: 5", "Leo: 8", "Emma: 13", "Lily: 15"]
        wrong = "Chloe: 5 , Leo: 8 , Emma: 13 , Lily: 14"  # Lily should be 15
        self.assertFalse(self._grade(accepted, wrong))

    # --- the essence of the bug: prefix must NOT match --------------------------
    def test_regex_prefix_does_not_match(self):
        # pattern "\d+" used to accept "42abc" via re.match (prefix). fullmatch rejects.
        self.assertFalse(self._grade([r"\d+"], "42abc"))
        self.assertTrue(self._grade([r"\d+"], "42"))  # whole string is digits -> ok

    # --- no regression: an exact single accepted alternative still passes -------
    def test_exact_single_accepted_alternative_still_passes(self):
        accepted = ["Chloe: 5", "Leo: 8", "Emma: 13", "Lily: 15"]
        self.assertTrue(self._grade(accepted, "Leo: 8"))
