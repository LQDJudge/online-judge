from django.core.exceptions import ValidationError
from django.forms import modelformset_factory
from django.test import TestCase
from django.utils import timezone

from judge.models import (
    Contest,
    ContestProblem,
    Course,
    CourseLesson,
    CourseLessonProblem,
    CourseLessonQuiz,
    Language,
    Problem,
    ProblemGroup,
)
from judge.models.quiz import Quiz, QuizQuestion, QuizQuestionAssignment
from judge.utils.identity import SemanticIdentityModelFormSet, save_semantic_formset


class ImmutableIdentityRowsTest(TestCase):
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
            name="identity", defaults={"full_name": "Identity Tests"}
        )

    def make_problem(self, code):
        return Problem.objects.create(
            code=code,
            name=f"Problem {code}",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=100.0,
        )

    def make_quiz(self, code):
        return Quiz.objects.create(code=code, title=f"Quiz {code}")

    def make_question(self, title):
        return QuizQuestion.objects.create(
            question_type="MC",
            title=title,
            content="Question content",
            choices=[{"id": "a", "text": "A"}],
            correct_answers={"answers": "a"},
        )

    def test_contest_problem_problem_identity_is_immutable(self):
        problem = self.make_problem("immutable-cp-1")
        replacement = self.make_problem("immutable-cp-2")
        contest = Contest.objects.create(
            key="immutablecp",
            name="Immutable Contest",
            start_time=timezone.now(),
            end_time=timezone.now() + timezone.timedelta(hours=2),
        )
        contest_problem = ContestProblem.objects.create(
            contest=contest, problem=problem, points=100, order=1
        )

        contest_problem.problem = replacement
        with self.assertRaises(ValidationError):
            contest_problem.save()

        contest_problem.refresh_from_db()
        self.assertEqual(contest_problem.problem, problem)

    def test_course_lesson_problem_identity_is_immutable(self):
        problem = self.make_problem("immutable-lp-1")
        replacement = self.make_problem("immutable-lp-2")
        course = Course.objects.create(
            name="Immutable Course", slug="immutable-course", about=""
        )
        lesson = CourseLesson.objects.create(
            course=course, title="Lesson", content="", order=1, points=100
        )
        lesson_problem = CourseLessonProblem.objects.create(
            lesson=lesson, problem=problem, order=1, score=100
        )

        lesson_problem.problem = replacement
        with self.assertRaises(ValidationError):
            lesson_problem.save()

        lesson_problem.refresh_from_db()
        self.assertEqual(lesson_problem.problem, problem)

    def test_course_lesson_quiz_identity_is_immutable(self):
        quiz = self.make_quiz("immutable-lq-1")
        replacement = self.make_quiz("immutable-lq-2")
        course = Course.objects.create(
            name="Immutable Quiz Course", slug="immutable-quiz-course", about=""
        )
        lesson = CourseLesson.objects.create(
            course=course, title="Lesson", content="", order=1, points=100
        )
        lesson_quiz = CourseLessonQuiz.objects.create(
            lesson=lesson, quiz=quiz, points=100, order=1
        )

        lesson_quiz.quiz = replacement
        with self.assertRaises(ValidationError):
            lesson_quiz.save()

        lesson_quiz.refresh_from_db()
        self.assertEqual(lesson_quiz.quiz, quiz)

    def test_quiz_question_assignment_identity_is_immutable(self):
        quiz = self.make_quiz("immutable-assignment")
        question = self.make_question("Question 1")
        replacement = self.make_question("Question 2")
        assignment = QuizQuestionAssignment.objects.create(
            quiz=quiz, question=question, points=1, order=1
        )

        assignment.question = replacement
        with self.assertRaises(ValidationError):
            assignment.save()

        assignment.refresh_from_db()
        self.assertEqual(assignment.question, question)

    def test_semantic_formset_populates_saved_object_bookkeeping(self):
        first_problem = self.make_problem("semantic-bookkeeping-1")
        second_problem = self.make_problem("semantic-bookkeeping-2")
        contest = Contest.objects.create(
            key="semanticbookkeeping",
            name="Semantic Bookkeeping",
            start_time=timezone.now(),
            end_time=timezone.now() + timezone.timedelta(hours=2),
        )
        contest_problem = ContestProblem.objects.create(
            contest=contest, problem=first_problem, points=100, order=1
        )
        formset_class = modelformset_factory(
            ContestProblem,
            fields=("order", "problem", "points", "partial"),
            extra=1,
            can_delete=True,
        )
        formset = formset_class(
            data={
                "form-TOTAL_FORMS": "2",
                "form-INITIAL_FORMS": "1",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-id": str(contest_problem.id),
                "form-0-order": "2",
                "form-0-problem": str(first_problem.id),
                "form-0-points": "100",
                "form-1-id": "",
                "form-1-order": "1",
                "form-1-problem": str(second_problem.id),
                "form-1-points": "100",
            },
            queryset=ContestProblem.objects.filter(contest=contest).order_by("order"),
        )

        self.assertTrue(formset.is_valid())
        save_semantic_formset(
            formset,
            parent_field="contest",
            parent=contest,
            identity_fields=("problem", "quiz"),
        )

        self.assertEqual([obj.problem for obj in formset.new_objects], [second_problem])
        self.assertEqual(formset.changed_objects[0][0], contest_problem)
        self.assertIn("order", formset.changed_objects[0][1])
        self.assertEqual(formset.deleted_objects, [])

    def test_semantic_formset_duplicate_identity_is_validation_error(self):
        problem = self.make_problem("semantic-duplicate")

        class LessonProblemFormSet(SemanticIdentityModelFormSet):
            semantic_identity_fields = ("problem",)

        formset_class = modelformset_factory(
            CourseLessonProblem,
            fields=("order", "problem", "score"),
            formset=LessonProblemFormSet,
            extra=2,
        )
        formset = formset_class(
            data={
                "form-TOTAL_FORMS": "2",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-order": "1",
                "form-0-problem": str(problem.id),
                "form-0-score": "100",
                "form-1-order": "2",
                "form-1-problem": str(problem.id),
                "form-1-score": "100",
            },
            queryset=CourseLessonProblem.objects.none(),
        )

        self.assertFalse(formset.is_valid())
        self.assertEqual(
            list(formset.forms[0].non_field_errors()),
            ["Duplicate rows are not allowed."],
        )
        self.assertEqual(
            list(formset.forms[1].non_field_errors()),
            ["Duplicate rows are not allowed."],
        )
