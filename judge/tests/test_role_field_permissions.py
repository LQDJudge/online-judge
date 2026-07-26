import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone, translation

from judge.forms import ProblemEditForm
from judge.models import Language, Problem, ProblemGroup, Profile
from judge.models.quiz import Quiz, QuizQuestion
from judge.views.quiz import QuizEditForm, QuizQuestionForm


class AuthorManagedRoleFieldsTest(TestCase):
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
            name="rolefields", defaults={"full_name": "Role Fields"}
        )

    def setUp(self):
        self.author = self._make_user("role_author")
        self.curator = self._make_user("role_curator")
        self.tester = self._make_user("role_tester")
        self.outsider = self._make_user("role_outsider")
        self.superuser = self._make_user("role_superuser", is_superuser=True)

    def _make_user(self, username, *, is_superuser=False):
        user = User.objects.create_user(username=username, password="password123")
        if is_superuser:
            user.is_superuser = True
            user.is_staff = True
            user.save()
        Profile.objects.get_or_create(user=user, defaults={"language": self.language})
        return user

    def _make_problem(self):
        problem = Problem.objects.create(
            code="roleproblem",
            name="Role Problem",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=1.0,
        )
        problem.allowed_languages.add(self.language)
        problem.authors.add(self.author.profile)
        problem.curators.add(self.curator.profile)
        problem.testers.add(self.tester.profile)
        return problem

    def _problem_post_data(self, problem, **overrides):
        data = {
            "code": problem.code,
            "name": problem.name,
            "is_public": "",
            "organizations": [],
            "date": timezone.now().strftime("%Y-%m-%d %H:%M:%S"),
            "authors": [str(p.id) for p in problem.authors.all()],
            "curators": [str(p.id) for p in problem.curators.all()],
            "testers": [str(p.id) for p in problem.testers.all()],
            "description": problem.description,
            "types": [],
            "group": str(self.problem_group.id),
            "points": str(problem.points),
            "time_limit": "1.0",
            "memory_limit": "64",
            "memory_unit": "MB",
            "allowed_languages": [str(self.language.id)],
        }
        data.update(overrides)
        return data

    def _make_quiz(self):
        quiz = Quiz.objects.create(code="rolequiz", title="Role Quiz")
        quiz.authors.add(self.author.profile)
        quiz.curators.add(self.curator.profile)
        quiz.testers.add(self.tester.profile)
        return quiz

    def _quiz_post_data(self, quiz, **overrides):
        data = {
            "code": quiz.code,
            "title": quiz.title,
            "description": quiz.description,
            "time_limit": str(quiz.time_limit),
            "shuffle_questions": "on" if quiz.shuffle_questions else "",
            "is_shown_correctness": "on" if quiz.is_shown_correctness else "",
            "is_shown_answer": "on" if quiz.is_shown_answer else "",
            "is_public": "on" if quiz.is_public else "",
            "authors": [str(p.id) for p in quiz.authors.all()],
            "curators": [str(p.id) for p in quiz.curators.all()],
            "testers": [str(p.id) for p in quiz.testers.all()],
        }
        data.update(overrides)
        return data

    def _make_question(self):
        question = QuizQuestion.objects.create(
            question_type="MC",
            title="Role Question",
            content="What is 2 + 2?",
            choices=[{"id": "a", "text": "4"}],
            correct_answers={"answers": "a"},
        )
        question.authors.add(self.author.profile)
        question.curators.add(self.curator.profile)
        return question

    def _question_post_data(self, question, **overrides):
        data = {
            "title": question.title,
            "question_type": question.question_type,
            "content": question.content,
            "choices": json.dumps(question.choices),
            "correct_answers": json.dumps(question.correct_answers),
            "grading_strategy": question.grading_strategy,
            "shuffle_choices": "on" if question.shuffle_choices else "",
            "tags": question.tags,
            "is_public": "on" if question.is_public else "",
            "explanation": question.explanation,
            "authors": [str(p.id) for p in question.authors.all()],
            "curators": [str(p.id) for p in question.curators.all()],
        }
        data.update(overrides)
        return data

    def test_problem_author_can_edit_role_fields(self):
        problem = self._make_problem()

        form = ProblemEditForm(
            instance=problem,
            user=self.author,
            profile=self.author.profile,
        )

        self.assertFalse(form.fields["authors"].disabled)
        self.assertFalse(form.fields["curators"].disabled)
        self.assertFalse(form.fields["testers"].disabled)

    def test_problem_curator_cannot_edit_role_fields(self):
        problem = self._make_problem()

        form = ProblemEditForm(
            instance=problem,
            user=self.curator,
            profile=self.curator.profile,
        )

        self.assertTrue(form.fields["authors"].disabled)
        self.assertTrue(form.fields["curators"].disabled)
        self.assertTrue(form.fields["testers"].disabled)

    def test_problem_curator_forged_role_post_preserves_existing_roles(self):
        problem = self._make_problem()
        post = self._problem_post_data(
            problem,
            name="Curator Content Edit",
            authors=[str(self.outsider.profile.id)],
            curators=[],
            testers=[str(self.outsider.profile.id)],
        )

        form = ProblemEditForm(
            data=post,
            instance=problem,
            user=self.curator,
            profile=self.curator.profile,
        )

        self.assertTrue(form.is_valid(), form.errors)
        updated = form.save(commit=False)
        updated.save()
        form.save_m2m()
        problem.refresh_from_db()
        self.assertEqual(problem.name, "Curator Content Edit")
        self.assertQuerySetEqual(
            problem.authors.order_by("id"),
            [self.author.profile],
            transform=lambda profile: profile,
        )
        self.assertQuerySetEqual(
            problem.curators.order_by("id"),
            [self.curator.profile],
            transform=lambda profile: profile,
        )
        self.assertQuerySetEqual(
            problem.testers.order_by("id"),
            [self.tester.profile],
            transform=lambda profile: profile,
        )

    def test_force_enabled_problem_curator_role_change_is_rejected(self):
        problem = self._make_problem()
        post = self._problem_post_data(problem, authors=[str(self.outsider.profile.id)])

        with translation.override("en"):
            form = ProblemEditForm(
                data=post,
                instance=problem,
                user=self.curator,
                profile=self.curator.profile,
            )
            form.fields["authors"].disabled = False
            self.assertFalse(form.is_valid())
            self.assertIn("Only problem authors", str(form.errors))

    def test_quiz_author_can_edit_role_fields(self):
        quiz = self._make_quiz()

        form = QuizEditForm(instance=quiz, user=self.author)

        self.assertFalse(form.fields["authors"].disabled)
        self.assertFalse(form.fields["curators"].disabled)
        self.assertFalse(form.fields["testers"].disabled)

    def test_quiz_curator_cannot_edit_role_fields(self):
        quiz = self._make_quiz()

        form = QuizEditForm(instance=quiz, user=self.curator)

        self.assertTrue(form.fields["authors"].disabled)
        self.assertTrue(form.fields["curators"].disabled)
        self.assertTrue(form.fields["testers"].disabled)

    def test_quiz_curator_forged_role_post_preserves_existing_roles(self):
        quiz = self._make_quiz()
        post = self._quiz_post_data(
            quiz,
            title="Curator Quiz Edit",
            authors=[str(self.outsider.profile.id)],
            curators=[],
            testers=[str(self.outsider.profile.id)],
        )

        form = QuizEditForm(data=post, instance=quiz, user=self.curator)

        self.assertTrue(form.is_valid(), form.errors)
        updated = form.save(commit=False)
        updated.save()
        form.save_m2m()
        quiz.refresh_from_db()
        self.assertEqual(quiz.title, "Curator Quiz Edit")
        self.assertQuerySetEqual(
            quiz.authors.order_by("id"),
            [self.author.profile],
            transform=lambda profile: profile,
        )
        self.assertQuerySetEqual(
            quiz.curators.order_by("id"),
            [self.curator.profile],
            transform=lambda profile: profile,
        )
        self.assertQuerySetEqual(
            quiz.testers.order_by("id"),
            [self.tester.profile],
            transform=lambda profile: profile,
        )

    def test_force_enabled_quiz_curator_role_change_is_rejected(self):
        quiz = self._make_quiz()
        post = self._quiz_post_data(quiz, authors=[str(self.outsider.profile.id)])

        with translation.override("en"):
            form = QuizEditForm(data=post, instance=quiz, user=self.curator)
            form.fields["authors"].disabled = False
            self.assertFalse(form.is_valid())
            self.assertIn("Only quiz authors", str(form.errors))

    def test_quiz_question_curator_role_fields_are_guarded_if_exposed(self):
        question = self._make_question()

        class QuizQuestionRoleForm(QuizQuestionForm):
            class Meta(QuizQuestionForm.Meta):
                fields = QuizQuestionForm.Meta.fields + ["authors", "curators"]

        form = QuizQuestionRoleForm(instance=question, user=self.curator)
        self.assertTrue(form.fields["authors"].disabled)
        self.assertTrue(form.fields["curators"].disabled)

        post = self._question_post_data(
            question, authors=[str(self.outsider.profile.id)]
        )
        with translation.override("en"):
            form = QuizQuestionRoleForm(data=post, instance=question, user=self.curator)
            form.fields["authors"].disabled = False
            self.assertFalse(form.is_valid())
            self.assertIn("Only question authors", str(form.errors))
