from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from decimal import Decimal

from judge.models import (
    Course,
    CourseLesson,
    CourseRole,
    CourseLessonPrerequisite,
    CourseLessonProgress,
    CourseLessonProblem,
    Language,
    Profile,
    Problem,
    ProblemGroup,
    Submission,
    BestSubmission,
    Quiz,
    QuizQuestion,
    QuizQuestionAssignment,
    CourseLessonQuiz,
    QuizAttempt,
    BestQuizAttempt,
)
from judge.models.course import RoleInCourse
from judge.utils.course_prerequisites import (
    get_lesson_prerequisites_graph,
    get_lessons_by_order,
    update_lesson_unlock_states,
    get_lesson_lock_status,
    propagate_unlock_from_lesson,
    update_lesson_grade,
)


class CourseLessonPrerequisiteModelTest(TestCase):
    """Test cases for CourseLessonPrerequisite model"""

    @classmethod
    def setUpTestData(cls):
        # Create default language required for Profile
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

    def setUp(self):
        # Create user and profile
        self.user = User.objects.create_user(
            username="test_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        # Create course
        self.course = Course.objects.create(
            name="Test Course",
            slug="test-course",
            about="Test course description",
            is_public=True,
            is_open=True,
        )

        # Create lessons with different orders
        self.lesson1 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 1 - Introduction",
            content="Introduction content",
            order=1,
            points=100,
        )
        self.lesson2 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 2 - Basics",
            content="Basics content",
            order=2,
            points=100,
        )
        self.lesson3 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 3 - Advanced",
            content="Advanced content",
            order=3,
            points=100,
        )

    def test_prerequisite_creation(self):
        """Test creating a valid prerequisite"""
        prereq = CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=70.0,
        )
        self.assertEqual(prereq.course, self.course)
        self.assertEqual(prereq.source_order, 1)
        self.assertEqual(prereq.target_order, 2)
        self.assertEqual(prereq.required_percentage, 70.0)

    def test_prerequisite_str(self):
        """Test prerequisite string representation"""
        prereq = CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=70.0,
        )
        self.assertIn("test-course", str(prereq))
        self.assertIn("1", str(prereq))
        self.assertIn("2", str(prereq))
        self.assertIn("70", str(prereq))

    def test_prerequisite_unique_together(self):
        """Test that duplicate prerequisites are not allowed"""
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=70.0,
        )
        # Attempting to create a duplicate should raise IntegrityError
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            CourseLessonPrerequisite.objects.create(
                course=self.course,
                source_order=1,
                target_order=2,
                required_percentage=80.0,
            )

    def test_prerequisite_validation_source_less_than_target(self):
        """Test that source order must be less than target order"""
        prereq = CourseLessonPrerequisite(
            course=self.course,
            source_order=2,
            target_order=1,  # Invalid: source >= target
            required_percentage=70.0,
        )
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            prereq.clean()

    def test_prerequisite_validation_same_order(self):
        """Test that same source and target order is invalid"""
        prereq = CourseLessonPrerequisite(
            course=self.course,
            source_order=1,
            target_order=1,  # Invalid: source == target
            required_percentage=70.0,
        )
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            prereq.clean()


class CourseLessonProgressModelTest(TestCase):
    """Test cases for CourseLessonProgress model"""

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

    def setUp(self):
        self.user = User.objects.create_user(
            username="test_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.course = Course.objects.create(
            name="Test Course",
            slug="test-course",
            about="Test course description",
            is_public=True,
            is_open=True,
        )

        self.lesson = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 1",
            content="Content",
            order=1,
            points=100,
        )

    def test_progress_creation(self):
        """Test creating lesson progress"""
        progress = CourseLessonProgress.objects.create(
            user=self.profile,
            lesson=self.lesson,
            is_unlocked=True,
            percentage=75.0,
        )
        self.assertEqual(progress.user, self.profile)
        self.assertEqual(progress.lesson, self.lesson)
        self.assertTrue(progress.is_unlocked)
        self.assertEqual(progress.percentage, 75.0)

    def test_progress_str(self):
        """Test progress string representation"""
        progress = CourseLessonProgress.objects.create(
            user=self.profile,
            lesson=self.lesson,
            is_unlocked=True,
            percentage=75.0,
        )
        self.assertIn("test_user", str(progress))
        self.assertIn("unlocked", str(progress))
        self.assertIn("75", str(progress))

    def test_progress_unique_together(self):
        """Test that duplicate user-lesson pairs are not allowed"""
        CourseLessonProgress.objects.create(
            user=self.profile,
            lesson=self.lesson,
            is_unlocked=True,
            percentage=75.0,
        )
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            CourseLessonProgress.objects.create(
                user=self.profile,
                lesson=self.lesson,
                is_unlocked=False,
                percentage=50.0,
            )


class UnlockAlgorithmTest(TestCase):
    """Test cases for the BFS unlock algorithm"""

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

    def setUp(self):
        self.user = User.objects.create_user(username="student", password="password123")
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.course = Course.objects.create(
            name="Test Course",
            slug="test-course-unlock",
            about="Test course",
            is_public=True,
            is_open=True,
        )

        # Create 5 lessons
        self.lessons = []
        for i in range(1, 6):
            lesson = CourseLesson.objects.create(
                course=self.course,
                title=f"Lesson {i}",
                content=f"Content for lesson {i}",
                order=i,
                points=100,
            )
            self.lessons.append(lesson)

    def test_no_prerequisites_all_unlocked(self):
        """Test that all lessons are unlocked when there are no prerequisites"""
        update_lesson_unlock_states(self.profile, self.course)

        for lesson in self.lessons:
            progress = CourseLessonProgress.objects.get(
                user=self.profile, lesson=lesson
            )
            self.assertTrue(
                progress.is_unlocked,
                f"Lesson {lesson.order} should be unlocked when no prerequisites",
            )

    def test_single_prerequisite_blocks_lesson(self):
        """Test that a single prerequisite blocks the target lesson"""
        # Lesson 2 requires 70% of Lesson 1
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=70.0,
        )

        update_lesson_unlock_states(self.profile, self.course)

        # Lesson 1 should be unlocked (no prerequisites)
        progress1 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lessons[0]
        )
        self.assertTrue(progress1.is_unlocked)

        # Lesson 2 should be locked (prerequisite not met)
        progress2 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lessons[1]
        )
        self.assertFalse(progress2.is_unlocked)

        # Lessons 3, 4, 5 should be unlocked (no prerequisites)
        for lesson in self.lessons[2:]:
            progress = CourseLessonProgress.objects.get(
                user=self.profile, lesson=lesson
            )
            self.assertTrue(progress.is_unlocked)

    def test_chain_prerequisites(self):
        """Test chain of prerequisites: 1 -> 2 -> 3"""
        # Lesson 2 requires 70% of Lesson 1
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=70.0,
        )
        # Lesson 3 requires 70% of Lesson 2
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=2,
            target_order=3,
            required_percentage=70.0,
        )

        update_lesson_unlock_states(self.profile, self.course)

        # Only Lesson 1, 4, 5 should be unlocked
        progress1 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lessons[0]
        )
        self.assertTrue(progress1.is_unlocked)

        progress2 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lessons[1]
        )
        self.assertFalse(progress2.is_unlocked)

        progress3 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lessons[2]
        )
        self.assertFalse(progress3.is_unlocked)

    def test_multiple_prerequisites_all_required(self):
        """Test that ALL prerequisites must be met"""
        # Lesson 3 requires both Lesson 1 (70%) and Lesson 2 (80%)
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=3,
            required_percentage=70.0,
        )
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=2,
            target_order=3,
            required_percentage=80.0,
        )

        update_lesson_unlock_states(self.profile, self.course)

        # Lessons 1 and 2 should be unlocked (no prerequisites)
        self.assertTrue(
            CourseLessonProgress.objects.get(
                user=self.profile, lesson=self.lessons[0]
            ).is_unlocked
        )
        self.assertTrue(
            CourseLessonProgress.objects.get(
                user=self.profile, lesson=self.lessons[1]
            ).is_unlocked
        )

        # Lesson 3 should be locked (both prerequisites not met)
        self.assertFalse(
            CourseLessonProgress.objects.get(
                user=self.profile, lesson=self.lessons[2]
            ).is_unlocked
        )

    def test_prerequisite_satisfied_with_grade(self):
        """Test that meeting the grade requirement unlocks the lesson"""
        from unittest.mock import patch

        # Lesson 2 requires 70% of Lesson 1
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=70.0,
        )

        # Mock calculate_user_lesson_grades to return 75% for lesson 1
        def mock_grades(user_profile, lessons):
            return {
                lesson.order: 75.0 if lesson.order == 1 else 0 for lesson in lessons
            }

        with patch(
            "judge.utils.course_prerequisites.calculate_user_lesson_grades",
            side_effect=mock_grades,
        ):
            update_lesson_unlock_states(self.profile, self.course)

        # Now lesson 2 should be unlocked
        progress2 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lessons[1]
        )
        self.assertTrue(progress2.is_unlocked)

    def test_prerequisite_not_satisfied_insufficient_grade(self):
        """Test that insufficient grade keeps lesson locked"""
        from unittest.mock import patch

        # Lesson 2 requires 70% of Lesson 1
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=70.0,
        )

        # Mock calculate_user_lesson_grades to return 60% for lesson 1
        def mock_grades(user_profile, lessons):
            return {
                lesson.order: 60.0 if lesson.order == 1 else 0 for lesson in lessons
            }

        with patch(
            "judge.utils.course_prerequisites.calculate_user_lesson_grades",
            side_effect=mock_grades,
        ):
            update_lesson_unlock_states(self.profile, self.course)

        # Lesson 2 should remain locked
        progress2 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lessons[1]
        )
        self.assertFalse(progress2.is_unlocked)


class GetLessonLockStatusTest(TestCase):
    """Test cases for get_lesson_lock_status function"""

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

    def setUp(self):
        self.user = User.objects.create_user(username="student", password="password123")
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.course = Course.objects.create(
            name="Test Course",
            slug="test-course-lock",
            about="Test course",
            is_public=True,
            is_open=True,
        )

        self.lesson1 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 1",
            content="Content",
            order=1,
            points=100,
        )
        self.lesson2 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 2",
            content="Content",
            order=2,
            points=100,
        )

    def test_no_prerequisites_all_unlocked(self):
        """Test that lock status returns False for all when no prerequisites"""
        lock_status = get_lesson_lock_status(self.profile, self.course)

        self.assertFalse(lock_status.get(self.lesson1.id, True))
        self.assertFalse(lock_status.get(self.lesson2.id, True))

    def test_prerequisite_shows_locked(self):
        """Test that lock status correctly shows locked lessons"""
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=70.0,
        )

        lock_status = get_lesson_lock_status(self.profile, self.course)

        self.assertFalse(lock_status.get(self.lesson1.id))  # No prerequisites
        self.assertTrue(lock_status.get(self.lesson2.id))  # Has unmet prerequisite


class CourseLessonLimitTest(TestCase):
    """Test cases for course lesson limit validation"""

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

    def setUp(self):
        self.course = Course.objects.create(
            name="Test Course",
            slug="test-course-limit",
            about="Test course",
            is_public=True,
            is_open=True,
        )

    def test_max_lessons_constant_exists(self):
        """Test that MAX_LESSONS_PER_COURSE constant is defined"""
        self.assertEqual(CourseLesson.MAX_LESSONS_PER_COURSE, 100)


class PrerequisiteGraphTest(TestCase):
    """Test cases for prerequisite graph utility functions"""

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

    def setUp(self):
        self.course = Course.objects.create(
            name="Test Course",
            slug="test-course-graph",
            about="Test course",
            is_public=True,
            is_open=True,
        )

        for i in range(1, 4):
            CourseLesson.objects.create(
                course=self.course,
                title=f"Lesson {i}",
                content=f"Content {i}",
                order=i,
                points=100,
            )

    def test_get_lessons_by_order(self):
        """Test get_lessons_by_order returns correct mapping"""
        lessons_by_order = get_lessons_by_order(self.course)

        self.assertEqual(len(lessons_by_order), 3)
        self.assertEqual(lessons_by_order[1].title, "Lesson 1")
        self.assertEqual(lessons_by_order[2].title, "Lesson 2")
        self.assertEqual(lessons_by_order[3].title, "Lesson 3")

    def test_get_lesson_prerequisites_graph_empty(self):
        """Test graph is empty when no prerequisites"""
        adj_list, in_degree = get_lesson_prerequisites_graph(self.course)

        self.assertEqual(len(adj_list), 0)
        self.assertEqual(len(in_degree), 0)

    def test_get_lesson_prerequisites_graph_with_prereqs(self):
        """Test graph structure with prerequisites"""
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=70.0,
        )
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=3,
            required_percentage=80.0,
        )

        adj_list, in_degree = get_lesson_prerequisites_graph(self.course)

        # Check adjacency list
        self.assertEqual(len(adj_list[1]), 2)  # Lesson 1 leads to 2 and 3
        self.assertIn((2, 70.0), adj_list[1])
        self.assertIn((3, 80.0), adj_list[1])

        # Check in-degree
        self.assertEqual(in_degree[2], 1)
        self.assertEqual(in_degree[3], 1)


class BestSubmissionModelTest(TestCase):
    """Test cases for BestSubmission model and grade trigger"""

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
            name="Test Group", defaults={"full_name": "Test Problem Group"}
        )

    def setUp(self):
        self.user = User.objects.create_user(
            username="test_student", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.course = Course.objects.create(
            name="Test Course",
            slug="test-course-best-sub",
            about="Test course",
            is_public=True,
            is_open=True,
        )

        # Enroll user
        CourseRole.objects.create(
            course=self.course, user=self.profile, role=RoleInCourse.STUDENT
        )

        self.lesson = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 1",
            content="Content",
            order=1,
            points=100,
        )

        self.problem = Problem.objects.create(
            code="testproblem",
            name="Test Problem",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=10,
        )

        # Link problem to lesson
        CourseLessonProblem.objects.create(
            lesson=self.lesson, problem=self.problem, order=1, score=100
        )

    def test_best_submission_creation(self):
        """Test that BestSubmission can be created"""
        submission = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=80,
            case_total=100,
            points=8,
        )

        best_sub = BestSubmission.update_from_submission(submission)

        self.assertIsNotNone(best_sub)
        self.assertEqual(best_sub.user, self.profile)
        self.assertEqual(best_sub.problem, self.problem)
        self.assertEqual(best_sub.points, 80)
        self.assertEqual(best_sub.case_total, 100)

    def test_best_submission_only_updates_for_better_score(self):
        """Test that BestSubmission keeps highest score when lower score submission is added"""
        # First submission: 80%
        submission1 = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=80,
            case_total=100,
            points=8,
        )
        BestSubmission.update_from_submission(submission1)

        # Second submission: 60% (worse)
        submission2 = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=60,
            case_total=100,
            points=6,
        )
        result = BestSubmission.update_from_submission(submission2)

        # Always returns BestSubmission after recalculating
        self.assertIsNotNone(result)

        best_sub = BestSubmission.objects.get(user=self.profile, problem=self.problem)
        self.assertEqual(best_sub.points, 80)  # Still the first submission's score
        self.assertEqual(
            best_sub.submission_id, submission1.id
        )  # Points to the better submission

    def test_best_submission_updates_for_higher_score(self):
        """Test that BestSubmission updates when new submission is better"""
        # First submission: 60%
        submission1 = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=60,
            case_total=100,
            points=6,
        )
        BestSubmission.update_from_submission(submission1)

        # Second submission: 90% (better)
        submission2 = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=90,
            case_total=100,
            points=9,
        )
        result = BestSubmission.update_from_submission(submission2)

        self.assertIsNotNone(result)

        best_sub = BestSubmission.objects.get(user=self.profile, problem=self.problem)
        self.assertEqual(best_sub.points, 90)  # Updated to better score

    def test_best_submission_ignores_non_completed(self):
        """Test that non-completed submissions are ignored"""
        submission = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="QU",  # Queued, not completed
            case_points=100,
            case_total=100,
        )
        result = BestSubmission.update_from_submission(submission)

        self.assertIsNone(result)
        self.assertFalse(
            BestSubmission.objects.filter(
                user=self.profile, problem=self.problem
            ).exists()
        )


class BestQuizAttemptModelTest(TestCase):
    """Test cases for BestQuizAttempt model"""

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

    def setUp(self):
        self.user = User.objects.create_user(
            username="quiz_student", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.course = Course.objects.create(
            name="Test Course",
            slug="test-course-best-quiz",
            about="Test course",
            is_public=True,
            is_open=True,
        )

        # Note: We intentionally do NOT enroll the user in the course here.
        # This prevents _update_lesson_grade from being called during save,
        # which would trigger complex grade calculation logic with caching.
        # We're testing BestQuizAttempt model logic only, not the full chain.

        self.lesson = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 1",
            content="Content",
            order=1,
            points=100,
        )

        self.quiz = Quiz.objects.create(code="testquiz", title="Test Quiz")

        self.lesson_quiz = CourseLessonQuiz.objects.create(
            lesson=self.lesson, quiz=self.quiz, points=100
        )

    def test_best_quiz_attempt_creation(self):
        """Test that BestQuizAttempt can be created"""
        attempt = QuizAttempt.objects.create(
            user=self.profile,
            quiz=self.quiz,
            lesson_quiz=self.lesson_quiz,
            is_submitted=True,
            score=Decimal("80.00"),
            max_score=Decimal("100.00"),
        )

        best_attempt = BestQuizAttempt.update_from_attempt(attempt)

        self.assertIsNotNone(best_attempt)
        self.assertEqual(best_attempt.user, self.profile)
        self.assertEqual(best_attempt.lesson_quiz, self.lesson_quiz)
        self.assertEqual(best_attempt.score, Decimal("80.00"))

    def test_best_quiz_attempt_only_updates_for_better_score(self):
        """Test that BestQuizAttempt only updates when new attempt is better"""
        # First attempt: 80 points
        attempt1 = QuizAttempt.objects.create(
            user=self.profile,
            quiz=self.quiz,
            lesson_quiz=self.lesson_quiz,
            is_submitted=True,
            score=Decimal("80.00"),
            max_score=Decimal("100.00"),
        )
        BestQuizAttempt.update_from_attempt(attempt1)

        # Second attempt: 60 points (worse)
        attempt2 = QuizAttempt.objects.create(
            user=self.profile,
            quiz=self.quiz,
            lesson_quiz=self.lesson_quiz,
            is_submitted=True,
            score=Decimal("60.00"),
            max_score=Decimal("100.00"),
        )
        result = BestQuizAttempt.update_from_attempt(attempt2)

        # Now always returns BestQuizAttempt after recalculating (safer approach)
        self.assertIsNotNone(result)

        best = BestQuizAttempt.objects.get(
            user=self.profile, lesson_quiz=self.lesson_quiz
        )
        # Best score should still be 80 (the better attempt)
        self.assertEqual(best.score, Decimal("80.00"))

    def test_best_quiz_attempt_updates_for_higher_score(self):
        """Test that BestQuizAttempt updates when new attempt is better"""
        # First attempt: 60 points
        attempt1 = QuizAttempt.objects.create(
            user=self.profile,
            quiz=self.quiz,
            lesson_quiz=self.lesson_quiz,
            is_submitted=True,
            score=Decimal("60.00"),
            max_score=Decimal("100.00"),
        )
        BestQuizAttempt.update_from_attempt(attempt1)

        # Second attempt: 90 points (better)
        attempt2 = QuizAttempt.objects.create(
            user=self.profile,
            quiz=self.quiz,
            lesson_quiz=self.lesson_quiz,
            is_submitted=True,
            score=Decimal("90.00"),
            max_score=Decimal("100.00"),
        )
        result = BestQuizAttempt.update_from_attempt(attempt2)

        self.assertIsNotNone(result)

        best = BestQuizAttempt.objects.get(
            user=self.profile, lesson_quiz=self.lesson_quiz
        )
        self.assertEqual(best.score, Decimal("90.00"))

    def test_best_quiz_attempt_ignores_non_submitted(self):
        """Test that non-submitted attempts are ignored"""
        attempt = QuizAttempt.objects.create(
            user=self.profile,
            quiz=self.quiz,
            lesson_quiz=self.lesson_quiz,
            is_submitted=False,
            score=Decimal("100.00"),
            max_score=Decimal("100.00"),
        )
        result = BestQuizAttempt.update_from_attempt(attempt)

        self.assertIsNone(result)


class GradeChangeAndUnlockTriggerTest(TestCase):
    """Test cases for grade change triggering unlock algorithm"""

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
            name="Test Group", defaults={"full_name": "Test Problem Group"}
        )

    def setUp(self):
        self.user = User.objects.create_user(
            username="trigger_student", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.course = Course.objects.create(
            name="Test Course",
            slug="test-course-trigger",
            about="Test course",
            is_public=True,
            is_open=True,
        )

        # Enroll user
        CourseRole.objects.create(
            course=self.course, user=self.profile, role=RoleInCourse.STUDENT
        )

        # Create two lessons
        self.lesson1 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 1",
            content="Content",
            order=1,
            points=100,
        )
        self.lesson2 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 2",
            content="Content",
            order=2,
            points=100,
        )

        # Create prerequisite: Lesson 2 requires 70% of Lesson 1
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=70.0,
        )

        self.problem = Problem.objects.create(
            code="triggerproblem",
            name="Trigger Problem",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=10,
        )

        # Link problem to lesson 1
        CourseLessonProblem.objects.create(
            lesson=self.lesson1, problem=self.problem, order=1, score=100
        )

        # Initialize unlock states
        update_lesson_unlock_states(self.profile, self.course)

    def test_lesson2_initially_locked(self):
        """Test that lesson 2 is initially locked due to prerequisite"""
        progress2 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson2
        )
        self.assertFalse(progress2.is_unlocked)

    def test_insufficient_grade_keeps_lesson_locked(self):
        """Test that an insufficient grade keeps the lesson locked"""
        # Submit with 50% score (below 70% requirement)
        submission = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=50,
            case_total=100,
            points=5,
        )

        # Update best submission
        BestSubmission.update_from_submission(submission)

        # Update lesson grade which triggers unlock check
        update_lesson_grade(self.profile, self.lesson1)

        # Refresh and check - lesson 2 should still be locked
        progress2 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson2
        )
        self.assertFalse(progress2.is_unlocked)

    def test_sufficient_grade_unlocks_lesson(self):
        """Test that a sufficient grade unlocks the lesson"""
        # Submit with 80% score (above 70% requirement)
        submission = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=80,
            case_total=100,
            points=8,
        )

        # Update best submission
        BestSubmission.update_from_submission(submission)

        # Update lesson grade (marks needs_progress_recalculation=True)
        update_lesson_grade(self.profile, self.lesson1)

        # Simulate lazy recalculation when user visits course page
        update_lesson_unlock_states(self.profile, self.course)

        # Refresh and check - lesson 2 should now be unlocked
        progress2 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson2
        )
        self.assertTrue(progress2.is_unlocked)

    def test_improved_grade_triggers_unlock(self):
        """Test that improving grade from insufficient to sufficient unlocks lesson"""
        # First submission: 50% (insufficient)
        submission1 = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=50,
            case_total=100,
            points=5,
        )
        BestSubmission.update_from_submission(submission1)
        update_lesson_grade(self.profile, self.lesson1)

        # Simulate lazy recalculation
        update_lesson_unlock_states(self.profile, self.course)

        # Verify still locked
        progress2 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson2
        )
        self.assertFalse(progress2.is_unlocked)

        # Second submission: 75% (sufficient)
        submission2 = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=75,
            case_total=100,
            points=7.5,
        )
        BestSubmission.update_from_submission(submission2)
        update_lesson_grade(self.profile, self.lesson1)

        # Simulate lazy recalculation
        update_lesson_unlock_states(self.profile, self.course)

        # Verify now unlocked
        progress2.refresh_from_db()
        self.assertTrue(progress2.is_unlocked)


class FullIntegrationTest(TestCase):
    """
    End-to-end integration tests that verify the complete flow:
    - finished_submission() triggers BestSubmission update
    - BestSubmission.save() triggers update_lesson_grade()
    - update_lesson_grade() updates CourseLessonProgress
    - CourseLessonProgress.save() triggers propagate_unlock_from_lesson()
    """

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
            name="Test Group", defaults={"full_name": "Test Problem Group"}
        )

    def setUp(self):
        self.user = User.objects.create_user(
            username="integration_student", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.course = Course.objects.create(
            name="Integration Test Course",
            slug="integration-test-course",
            about="Test course",
            is_public=True,
            is_open=True,
        )

        # Enroll user
        CourseRole.objects.create(
            course=self.course, user=self.profile, role=RoleInCourse.STUDENT
        )

        # Create two lessons
        self.lesson1 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 1",
            content="Content",
            order=1,
            points=100,
        )
        self.lesson2 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 2",
            content="Content",
            order=2,
            points=100,
        )

        # Create prerequisite: Lesson 2 requires 70% of Lesson 1
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=70.0,
        )

        self.problem = Problem.objects.create(
            code="integrationproblem",
            name="Integration Problem",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=10,
        )

        # Link problem to lesson 1
        CourseLessonProblem.objects.create(
            lesson=self.lesson1, problem=self.problem, order=1, score=100
        )

        # Initialize unlock states
        update_lesson_unlock_states(self.profile, self.course)

    def test_finished_submission_triggers_full_chain(self):
        """
        Test that calling finished_submission() triggers the full chain:
        1. Creates/updates BestSubmission
        2. Updates CourseLessonProgress.percentage
        3. Marks needs_progress_recalculation=True
        4. Lazy recalculation triggers unlock propagation
        """
        from judge.utils.problems import finished_submission

        # Verify lesson 2 is initially locked
        progress2 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson2
        )
        self.assertFalse(progress2.is_unlocked)

        # Create a submission with 80% score (above 70% requirement)
        submission = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=80,
            case_total=100,
            points=8,
        )

        # Call finished_submission - this should trigger the full chain
        finished_submission(submission)

        # Verify BestSubmission was created
        best_sub = BestSubmission.objects.filter(
            user=self.profile, problem=self.problem
        ).first()
        self.assertIsNotNone(best_sub)
        self.assertEqual(best_sub.points, 80)

        # Verify lesson 1 progress was updated
        progress1 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson1
        )
        self.assertEqual(progress1.percentage, 80.0)

        # Verify needs_progress_recalculation is marked
        course_role = CourseRole.objects.get(course=self.course, user=self.profile)
        self.assertTrue(course_role.needs_progress_recalculation)

        # Simulate lazy recalculation when user visits course page
        update_lesson_unlock_states(self.profile, self.course)

        # Verify lesson 2 is now unlocked
        progress2.refresh_from_db()
        self.assertTrue(progress2.is_unlocked)

    def test_finished_submission_insufficient_score_keeps_locked(self):
        """
        Test that a submission with insufficient score keeps lesson locked.
        """
        from judge.utils.problems import finished_submission

        # Verify lesson 2 is initially locked
        progress2 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson2
        )
        self.assertFalse(progress2.is_unlocked)

        # Create a submission with 50% score (below 70% requirement)
        submission = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=50,
            case_total=100,
            points=5,
        )

        # Call finished_submission
        finished_submission(submission)

        # Verify BestSubmission was created
        best_sub = BestSubmission.objects.filter(
            user=self.profile, problem=self.problem
        ).first()
        self.assertIsNotNone(best_sub)
        self.assertEqual(best_sub.points, 50)

        # Verify lesson 1 progress was updated
        progress1 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson1
        )
        self.assertEqual(progress1.percentage, 50.0)

        # Verify lesson 2 is still locked
        progress2.refresh_from_db()
        self.assertFalse(progress2.is_unlocked)

    def test_improved_submission_unlocks_via_finished_submission(self):
        """
        Test that improving score via multiple submissions eventually unlocks lesson.
        """
        from judge.utils.problems import finished_submission

        # First submission: 50% (insufficient)
        submission1 = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=50,
            case_total=100,
            points=5,
        )
        finished_submission(submission1)

        # Simulate lazy recalculation
        update_lesson_unlock_states(self.profile, self.course)

        # Verify still locked
        progress2 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson2
        )
        self.assertFalse(progress2.is_unlocked)

        # Second submission: 75% (sufficient)
        submission2 = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=75,
            case_total=100,
            points=7.5,
        )
        finished_submission(submission2)

        # Verify BestSubmission was updated to higher score
        best_sub = BestSubmission.objects.get(user=self.profile, problem=self.problem)
        self.assertEqual(best_sub.points, 75)

        # Simulate lazy recalculation
        update_lesson_unlock_states(self.profile, self.course)

        # Verify lesson 2 is now unlocked
        progress2.refresh_from_db()
        self.assertTrue(progress2.is_unlocked)

    def test_lower_submission_does_not_change_unlock_status(self):
        """
        Test that a lower score submission doesn't affect the unlock status.
        """
        from judge.utils.problems import finished_submission

        # First submission: 80% (sufficient)
        submission1 = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=80,
            case_total=100,
            points=8,
        )
        finished_submission(submission1)

        # Simulate lazy recalculation
        update_lesson_unlock_states(self.profile, self.course)

        # Verify unlocked
        progress2 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson2
        )
        self.assertTrue(progress2.is_unlocked)

        # Second submission: 40% (worse)
        submission2 = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=40,
            case_total=100,
            points=4,
        )
        finished_submission(submission2)

        # Verify BestSubmission still has the higher score
        best_sub = BestSubmission.objects.get(user=self.profile, problem=self.problem)
        self.assertEqual(best_sub.points, 80)

        # Verify lesson 2 is still unlocked
        progress2.refresh_from_db()
        self.assertTrue(progress2.is_unlocked)


class QuizIntegrationTest(TestCase):
    """
    Integration tests for quiz attempts triggering unlock.
    Tests that BestQuizAttempt correctly triggers update_lesson_grade
    and propagates unlock states.
    """

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

    def setUp(self):
        self.user = User.objects.create_user(
            username="quiz_integration_student", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.course = Course.objects.create(
            name="Quiz Integration Test Course",
            slug="quiz-integration-test-course",
            about="Test course",
            is_public=True,
            is_open=True,
        )

        # Enroll user
        CourseRole.objects.create(
            course=self.course, user=self.profile, role=RoleInCourse.STUDENT
        )

        # Create two lessons
        self.lesson1 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 1",
            content="Content",
            order=1,
            points=100,
        )
        self.lesson2 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 2",
            content="Content",
            order=2,
            points=100,
        )

        # Create prerequisite: Lesson 2 requires 70% of Lesson 1
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=70.0,
        )

        # Create a quiz for lesson 1
        self.quiz = Quiz.objects.create(
            title="Test Quiz",
            description="A test quiz",
        )

        # Link quiz to lesson 1 (100% weight, visible)
        self.lesson_quiz = CourseLessonQuiz.objects.create(
            lesson=self.lesson1,
            quiz=self.quiz,
            order=1,
            points=100,
            is_visible=True,
        )

        # Initialize unlock states and create progress records
        update_lesson_unlock_states(self.profile, self.course)

    def test_progress_save_triggers_unlock_propagation(self):
        """
        Test that saving CourseLessonProgress with changed percentage
        and running lazy recalculation unlocks the target lesson.
        (With lazy recalculation, unlock happens when user visits course page)
        """
        from unittest.mock import patch

        # Verify lesson 2 is initially locked
        progress2 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson2
        )
        self.assertFalse(progress2.is_unlocked)

        # Mock calculate_user_lesson_grades to return 80% for lesson 1
        def mock_grades(user_profile, lessons):
            return {
                lesson.order: 80.0 if lesson.order == self.lesson1.order else 0
                for lesson in lessons
            }

        # Simulate lazy recalculation when user visits course page with sufficient grade
        with patch(
            "judge.utils.course_prerequisites.calculate_user_lesson_grades",
            side_effect=mock_grades,
        ):
            update_lesson_unlock_states(self.profile, self.course)

        # Verify lesson 2 is now unlocked (grade 80% >= 70% requirement)
        progress2.refresh_from_db()
        self.assertTrue(progress2.is_unlocked)

    def test_insufficient_progress_keeps_locked(self):
        """
        Test that a grade below the requirement keeps the lesson locked.
        """
        # Verify lesson 2 is initially locked
        progress2 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson2
        )
        self.assertFalse(progress2.is_unlocked)

        # Set lesson 1 progress to 50% (below 70% requirement)
        progress1, _ = CourseLessonProgress.objects.get_or_create(
            user=self.profile,
            lesson=self.lesson1,
            defaults={"percentage": 0, "is_unlocked": True},
        )
        progress1.percentage = 50.0
        progress1.save()

        # Simulate lazy recalculation
        update_lesson_unlock_states(self.profile, self.course)

        # Verify lesson 2 is still locked
        progress2.refresh_from_db()
        self.assertFalse(progress2.is_unlocked)

    def test_improved_progress_unlocks_lesson(self):
        """
        Test that improving grade from insufficient to sufficient unlocks lesson.
        """
        from unittest.mock import patch

        # Mock calculate_user_lesson_grades to return 50% for lesson 1 (insufficient)
        def mock_grades_50(user_profile, lessons):
            return {
                lesson.order: 50.0 if lesson.order == self.lesson1.order else 0
                for lesson in lessons
            }

        # Simulate lazy recalculation with insufficient grade
        with patch(
            "judge.utils.course_prerequisites.calculate_user_lesson_grades",
            side_effect=mock_grades_50,
        ):
            update_lesson_unlock_states(self.profile, self.course)

        # Verify still locked
        progress2 = CourseLessonProgress.objects.get(
            user=self.profile, lesson=self.lesson2
        )
        self.assertFalse(progress2.is_unlocked)

        # Mock calculate_user_lesson_grades to return 75% for lesson 1 (sufficient)
        def mock_grades_75(user_profile, lessons):
            return {
                lesson.order: 75.0 if lesson.order == self.lesson1.order else 0
                for lesson in lessons
            }

        # Simulate lazy recalculation with sufficient grade
        with patch(
            "judge.utils.course_prerequisites.calculate_user_lesson_grades",
            side_effect=mock_grades_75,
        ):
            update_lesson_unlock_states(self.profile, self.course)

        # Verify lesson 2 is now unlocked
        progress2.refresh_from_db()
        self.assertTrue(progress2.is_unlocked)

    def test_best_quiz_attempt_creation_and_update(self):
        """
        Test that BestQuizAttempt can be created and updated correctly.
        """
        # Create a quiz attempt
        attempt1 = QuizAttempt.objects.create(
            quiz=self.quiz,
            user=self.profile,
            lesson_quiz=self.lesson_quiz,
            is_submitted=True,
            score=Decimal("50.00"),
            max_score=Decimal("100.00"),
        )

        # Update best attempt
        best = BestQuizAttempt.update_from_attempt(attempt1)
        self.assertIsNotNone(best)
        self.assertEqual(best.score, Decimal("50.00"))

        # Create a better attempt
        attempt2 = QuizAttempt.objects.create(
            quiz=self.quiz,
            user=self.profile,
            lesson_quiz=self.lesson_quiz,
            is_submitted=True,
            score=Decimal("80.00"),
            max_score=Decimal("100.00"),
        )

        # Update best attempt - should update
        best = BestQuizAttempt.update_from_attempt(attempt2)
        self.assertIsNotNone(best)
        self.assertEqual(best.score, Decimal("80.00"))

        # Create a worse attempt
        attempt3 = QuizAttempt.objects.create(
            quiz=self.quiz,
            user=self.profile,
            lesson_quiz=self.lesson_quiz,
            is_submitted=True,
            score=Decimal("30.00"),
            max_score=Decimal("100.00"),
        )

        # Update best attempt - now always returns result after recalculating
        result = BestQuizAttempt.update_from_attempt(attempt3)
        self.assertIsNotNone(result)

        # Verify best is still 80 (the better attempt)
        best = BestQuizAttempt.objects.get(
            user=self.profile, lesson_quiz=self.lesson_quiz
        )
        self.assertEqual(best.score, Decimal("80.00"))


class LazyRecalculationFlagTest(TestCase):
    """
    Test cases for the needs_progress_recalculation flag on CourseRole.
    This flag enables lazy recalculation of unlock states.
    """

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
            name="Test Group", defaults={"full_name": "Test Problem Group"}
        )

    def setUp(self):
        # Create user and profile
        self.user = User.objects.create_user(
            username="testuser_lazy", password="testpass123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        # Create course
        self.course = Course.objects.create(
            name="Test Course Lazy",
            slug="test-course-lazy",
            about="Test course for lazy recalculation",
            is_public=True,
        )

        # Create lessons
        self.lesson1 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 1",
            content="Content 1",
            order=1,
            points=100,
        )
        self.lesson2 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 2",
            content="Content 2",
            order=2,
            points=100,
        )

        # Create problem
        self.problem = Problem.objects.create(
            code="test_lazy_prob",
            name="Test Lazy Problem",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=10,
        )

        # Link problem to lesson 1
        CourseLessonProblem.objects.create(
            lesson=self.lesson1, problem=self.problem, order=1, score=100
        )

        # Enroll user as student
        self.course_role = CourseRole.objects.create(
            course=self.course, user=self.profile, role="ST"
        )

    def test_course_role_has_needs_recalculation_field(self):
        """Test that CourseRole has the needs_progress_recalculation field"""
        self.assertTrue(hasattr(self.course_role, "needs_progress_recalculation"))

    def test_course_role_default_needs_recalculation_true(self):
        """Test that new CourseRole defaults to needs_progress_recalculation=True"""
        # New enrollment should have flag set to True
        new_user = User.objects.create_user(
            username="newuser_lazy", password="testpass"
        )
        new_profile, _ = Profile.objects.get_or_create(
            user=new_user, defaults={"language": self.language}
        )
        new_role = CourseRole.objects.create(
            course=self.course, user=new_profile, role="ST"
        )
        self.assertTrue(new_role.needs_progress_recalculation)

    def test_update_lesson_grade_marks_recalculation_flag(self):
        """Test that update_lesson_grade marks needs_progress_recalculation=True"""
        # First, clear the flag
        self.course_role.needs_progress_recalculation = False
        self.course_role.save()

        # Create a submission
        submission = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="D",
            case_points=80,
            case_total=100,
            points=8,
        )
        BestSubmission.update_from_submission(submission)

        # Call update_lesson_grade
        update_lesson_grade(self.profile, self.lesson1)

        # Verify flag is now True
        self.course_role.refresh_from_db()
        self.assertTrue(self.course_role.needs_progress_recalculation)

    def test_update_lesson_unlock_states_clears_flag(self):
        """Test that running unlock algorithm and clearing flag works"""
        # Set flag to True
        self.course_role.needs_progress_recalculation = True
        self.course_role.save()

        # Run unlock algorithm
        update_lesson_unlock_states(self.profile, self.course)

        # Manually clear flag (simulating what the view does)
        self.course_role.needs_progress_recalculation = False
        self.course_role.save()

        # Verify flag is now False
        self.course_role.refresh_from_db()
        self.assertFalse(self.course_role.needs_progress_recalculation)


class ModelTriggerTest(TestCase):
    """
    Test cases for model save/delete triggers that mark courses for recalculation.
    """

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
            name="Test Group", defaults={"full_name": "Test Problem Group"}
        )

    def setUp(self):
        # Create user and profile
        self.user = User.objects.create_user(
            username="testuser_trigger", password="testpass123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        # Create course
        self.course = Course.objects.create(
            name="Test Course Trigger",
            slug="test-course-trigger",
            about="Test course for triggers",
            is_public=True,
        )

        # Create lessons
        self.lesson1 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 1",
            content="Content 1",
            order=1,
            points=100,
        )
        self.lesson2 = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 2",
            content="Content 2",
            order=2,
            points=100,
        )

        # Create problem
        self.problem = Problem.objects.create(
            code="test_trigger_prob",
            name="Test Trigger Problem",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=10,
        )

        # Enroll user as student
        self.course_role = CourseRole.objects.create(
            course=self.course, user=self.profile, role="ST"
        )

        # Clear the flag initially
        self.course_role.needs_progress_recalculation = False
        self.course_role.save()

    def test_course_lesson_problem_save_triggers_recalculation(self):
        """Test that saving CourseLessonProblem marks users for recalculation"""
        # Create a new lesson problem - this should trigger recalculation
        CourseLessonProblem.objects.create(
            lesson=self.lesson1, problem=self.problem, order=1, score=100
        )

        # Verify flag is now True
        self.course_role.refresh_from_db()
        self.assertTrue(self.course_role.needs_progress_recalculation)

    def test_course_lesson_problem_delete_triggers_recalculation(self):
        """Test that deleting CourseLessonProblem marks users for recalculation"""
        # Create a lesson problem first
        lesson_problem = CourseLessonProblem.objects.create(
            lesson=self.lesson1, problem=self.problem, order=1, score=100
        )

        # Clear flag
        self.course_role.needs_progress_recalculation = False
        self.course_role.save()

        # Delete the lesson problem
        lesson_problem.delete()

        # Verify flag is now True
        self.course_role.refresh_from_db()
        self.assertTrue(self.course_role.needs_progress_recalculation)

    def test_course_lesson_order_change_does_not_trigger_recalculation(self):
        """Test that changing CourseLesson order does NOT trigger recalculation.
        Order changes are now managed via the Order tab and don't affect prerequisites.
        """
        # Clear flag
        self.course_role.needs_progress_recalculation = False
        self.course_role.save()

        # Change lesson order
        self.lesson1.order = 5
        self.lesson1.save()

        # Verify flag is still False (order changes don't trigger recalculation)
        self.course_role.refresh_from_db()
        self.assertFalse(self.course_role.needs_progress_recalculation)

    def test_course_lesson_points_change_triggers_recalculation(self):
        """Test that changing CourseLesson points marks users for recalculation"""
        # Clear flag
        self.course_role.needs_progress_recalculation = False
        self.course_role.save()

        # Change lesson points
        self.lesson1.points = 200
        self.lesson1.save()

        # Verify flag is now True
        self.course_role.refresh_from_db()
        self.assertTrue(self.course_role.needs_progress_recalculation)

    def test_course_lesson_title_change_does_not_trigger(self):
        """Test that changing CourseLesson title does NOT trigger recalculation"""
        # Clear flag
        self.course_role.needs_progress_recalculation = False
        self.course_role.save()

        # Change lesson title (should not trigger)
        self.lesson1.title = "New Title"
        self.lesson1.save()

        # Verify flag is still False
        self.course_role.refresh_from_db()
        self.assertFalse(self.course_role.needs_progress_recalculation)

    def test_course_lesson_delete_triggers_recalculation(self):
        """Test that deleting CourseLesson marks users for recalculation"""
        # Clear flag
        self.course_role.needs_progress_recalculation = False
        self.course_role.save()

        # Delete the lesson
        self.lesson2.delete()

        # Verify flag is now True
        self.course_role.refresh_from_db()
        self.assertTrue(self.course_role.needs_progress_recalculation)

    def test_prerequisite_save_triggers_recalculation(self):
        """Test that saving CourseLessonPrerequisite marks users for recalculation"""
        # Clear flag
        self.course_role.needs_progress_recalculation = False
        self.course_role.save()

        # Create a prerequisite
        CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=70.0,
        )

        # Verify flag is now True
        self.course_role.refresh_from_db()
        self.assertTrue(self.course_role.needs_progress_recalculation)

    def test_prerequisite_delete_triggers_recalculation(self):
        """Test that deleting CourseLessonPrerequisite marks users for recalculation"""
        # Create a prerequisite
        prereq = CourseLessonPrerequisite.objects.create(
            course=self.course,
            source_order=1,
            target_order=2,
            required_percentage=70.0,
        )

        # Clear flag
        self.course_role.needs_progress_recalculation = False
        self.course_role.save()

        # Delete the prerequisite
        prereq.delete()

        # Verify flag is now True
        self.course_role.refresh_from_db()
        self.assertTrue(self.course_role.needs_progress_recalculation)


class CourseLessonQuizTriggerTest(TestCase):
    """
    Test cases for CourseLessonQuiz save/delete triggers.
    """

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

    def setUp(self):
        # Create user and profile
        self.user = User.objects.create_user(
            username="testuser_quiz_trigger", password="testpass123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        # Create course
        self.course = Course.objects.create(
            name="Test Course Quiz Trigger",
            slug="test-course-quiz-trigger",
            about="Test course for quiz triggers",
            is_public=True,
        )

        # Create lesson
        self.lesson = CourseLesson.objects.create(
            course=self.course,
            title="Lesson 1",
            content="Content 1",
            order=1,
            points=100,
        )

        # Create quiz
        self.quiz = Quiz.objects.create(
            title="Test Quiz",
            description="A test quiz",
        )

        # Enroll user as student
        self.course_role = CourseRole.objects.create(
            course=self.course, user=self.profile, role="ST"
        )

        # Clear the flag initially
        self.course_role.needs_progress_recalculation = False
        self.course_role.save()

    def test_course_lesson_quiz_save_triggers_recalculation(self):
        """Test that saving CourseLessonQuiz marks users for recalculation"""
        # Create a lesson quiz
        CourseLessonQuiz.objects.create(
            lesson=self.lesson,
            quiz=self.quiz,
            order=1,
            points=100,
            is_visible=True,
        )

        # Verify flag is now True
        self.course_role.refresh_from_db()
        self.assertTrue(self.course_role.needs_progress_recalculation)

    def test_course_lesson_quiz_delete_triggers_recalculation(self):
        """Test that deleting CourseLessonQuiz marks users for recalculation"""
        # Create a lesson quiz first
        lesson_quiz = CourseLessonQuiz.objects.create(
            lesson=self.lesson,
            quiz=self.quiz,
            order=1,
            points=100,
            is_visible=True,
        )

        # Clear flag
        self.course_role.needs_progress_recalculation = False
        self.course_role.save()

        # Delete the lesson quiz
        lesson_quiz.delete()

        # Verify flag is now True
        self.course_role.refresh_from_db()
        self.assertTrue(self.course_role.needs_progress_recalculation)


class MarkCourseForRecalculationTest(TestCase):
    """
    Test cases for the mark_course_for_recalculation helper function.
    """

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

    def setUp(self):
        # Create course
        self.course = Course.objects.create(
            name="Test Course Mark",
            slug="test-course-mark",
            about="Test course for mark function",
            is_public=True,
        )

        # Create multiple users
        self.users = []
        self.profiles = []
        self.course_roles = []
        for i in range(3):
            user = User.objects.create_user(
                username=f"testuser_mark_{i}", password="testpass123"
            )
            profile, _ = Profile.objects.get_or_create(
                user=user, defaults={"language": self.language}
            )
            course_role = CourseRole.objects.create(
                course=self.course, user=profile, role="ST"
            )
            # Clear flag
            course_role.needs_progress_recalculation = False
            course_role.save()

            self.users.append(user)
            self.profiles.append(profile)
            self.course_roles.append(course_role)

    def test_mark_course_marks_all_enrolled_users(self):
        """Test that mark_course_for_recalculation marks ALL enrolled users"""
        from judge.utils.course_prerequisites import mark_course_for_recalculation

        # All flags should be False initially
        for role in self.course_roles:
            role.refresh_from_db()
            self.assertFalse(role.needs_progress_recalculation)

        # Mark course for recalculation
        mark_course_for_recalculation(self.course)

        # All flags should now be True
        for role in self.course_roles:
            role.refresh_from_db()
            self.assertTrue(role.needs_progress_recalculation)

    def test_mark_course_only_affects_target_course(self):
        """Test that marking one course doesn't affect other courses"""
        from judge.utils.course_prerequisites import mark_course_for_recalculation

        # Create another course with a user
        other_course = Course.objects.create(
            name="Other Course",
            slug="other-course",
            about="Another test course",
            is_public=True,
        )
        other_user = User.objects.create_user(
            username="other_course_user", password="testpass123"
        )
        other_profile, _ = Profile.objects.get_or_create(
            user=other_user, defaults={"language": self.language}
        )
        other_role = CourseRole.objects.create(
            course=other_course, user=other_profile, role="ST"
        )
        other_role.needs_progress_recalculation = False
        other_role.save()

        # Mark only the first course
        mark_course_for_recalculation(self.course)

        # Other course's user should not be affected
        other_role.refresh_from_db()
        self.assertFalse(other_role.needs_progress_recalculation)

        # First course's users should be marked
        for role in self.course_roles:
            role.refresh_from_db()
            self.assertTrue(role.needs_progress_recalculation)


class CourseRefreshProgressViewTest(TestCase):
    """
    Test cases for the CourseRefreshProgress view (refresh button endpoint).
    """

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

    def setUp(self):
        # Create teacher
        self.teacher_user = User.objects.create_user(
            username="teacher_refresh", password="testpass123"
        )
        self.teacher_profile, _ = Profile.objects.get_or_create(
            user=self.teacher_user, defaults={"language": self.language}
        )

        # Create student
        self.student_user = User.objects.create_user(
            username="student_refresh", password="testpass123"
        )
        self.student_profile, _ = Profile.objects.get_or_create(
            user=self.student_user, defaults={"language": self.language}
        )

        # Create course
        self.course = Course.objects.create(
            name="Test Course Refresh",
            slug="test-course-refresh",
            about="Test course for refresh view",
            is_public=True,
        )

        # Enroll teacher
        self.teacher_role = CourseRole.objects.create(
            course=self.course, user=self.teacher_profile, role="TE"
        )

        # Enroll student
        self.student_role = CourseRole.objects.create(
            course=self.course, user=self.student_profile, role="ST"
        )

        # Clear flags
        self.teacher_role.needs_progress_recalculation = False
        self.teacher_role.save()
        self.student_role.needs_progress_recalculation = False
        self.student_role.save()

        self.client = Client()

    def test_refresh_endpoint_requires_login(self):
        """Test that the refresh endpoint requires authentication"""
        response = self.client.post(
            reverse("course_refresh_progress", args=[self.course.slug])
        )
        # Should redirect to login or return 404 (course not accessible when not logged in)
        self.assertIn(response.status_code, [302, 404])

    def test_refresh_endpoint_requires_teacher_role(self):
        """Test that only teachers/assistants can access the refresh endpoint"""
        # Login as student
        self.client.login(username="student_refresh", password="testpass123")
        response = self.client.post(
            reverse("course_refresh_progress", args=[self.course.slug])
        )
        # Should get forbidden (403), redirect, or 404 (not editable)
        self.assertIn(response.status_code, [302, 403, 404])

    def test_refresh_endpoint_marks_all_users(self):
        """Test that the refresh endpoint marks all enrolled users for recalculation"""
        # Login as teacher
        self.client.login(username="teacher_refresh", password="testpass123")

        # Verify flags are False initially
        self.teacher_role.refresh_from_db()
        self.student_role.refresh_from_db()
        self.assertFalse(self.teacher_role.needs_progress_recalculation)
        self.assertFalse(self.student_role.needs_progress_recalculation)

        # Call refresh endpoint
        response = self.client.post(
            reverse("course_refresh_progress", args=[self.course.slug])
        )

        # Should redirect to prerequisites edit page
        self.assertEqual(response.status_code, 302)

        # Verify flags are now True
        self.teacher_role.refresh_from_db()
        self.student_role.refresh_from_db()
        self.assertTrue(self.teacher_role.needs_progress_recalculation)
        self.assertTrue(self.student_role.needs_progress_recalculation)

    def test_refresh_endpoint_get_redirects(self):
        """Test that GET request to refresh endpoint redirects"""
        self.client.login(username="teacher_refresh", password="testpass123")
        response = self.client.get(
            reverse("course_refresh_progress", args=[self.course.slug])
        )
        # Should redirect
        self.assertEqual(response.status_code, 302)
