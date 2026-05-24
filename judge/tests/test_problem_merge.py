from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from judge.models import (
    BestSubmission,
    BookMark,
    Comment,
    Contest,
    ContestParticipation,
    ContestProblem,
    ContestSubmission,
    Course,
    CourseLesson,
    CourseLessonProblem,
    Language,
    LanguageLimit,
    LanguageTemplate,
    PageVote,
    PageVoteVoter,
    Problem,
    ProblemAttachment,
    ProblemData,
    ProblemDuplicateCandidate,
    ProblemDuplicateReport,
    ProblemGroup,
    ProblemSolutionCode,
    ProblemTestCase,
    ProblemTranslation,
    ProblemType,
    Profile,
    PublicRequest,
    Solution,
    Submission,
    Ticket,
)
from judge.utils.problem_merge import ProblemMerge
from judge.ml.problem_duplicates import (
    DuplicateProblemMergePending,
    create_pending_duplicate_problem_merge,
    get_cached_duplicate_problem_candidates,
)


class ProblemMergeTestCase(TestCase):
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
            name="merge", defaults={"full_name": "Merge Tests"}
        )

    def setUp(self):
        self.user = User.objects.create_user("merge_user", password="password")
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )
        self.target = self.make_problem("target")
        self.source = self.make_problem("source")

    def make_problem(self, code, **kwargs):
        defaults = {
            "code": code,
            "name": f"Problem {code}",
            "description": "same statement",
            "group": self.problem_group,
            "time_limit": 1.0,
            "memory_limit": 65536,
            "points": 100.0,
            "is_public": True,
        }
        defaults.update(kwargs)
        return Problem.objects.create(**defaults)

    def make_submission(self, problem, *, user=None, points=100, result="AC"):
        return Submission.objects.create(
            user=user or self.profile,
            problem=problem,
            language=self.language,
            status="D",
            result=result,
            points=points,
            case_points=points,
            case_total=100,
            time=0.1,
            memory=1024,
        )

    def make_contest(self, key="merge_contest"):
        now = timezone.now()
        return Contest.objects.create(
            key=key,
            name=f"Contest {key}",
            start_time=now,
            end_time=now + timezone.timedelta(hours=2),
        )

    def test_dry_run_reports_without_mutating(self):
        source_submission = self.make_submission(self.source)
        report = ProblemMerge(self.source.code, self.target.code, apply=False).run()

        self.assertEqual(report["source"]["code"], self.source.code)
        self.assertEqual(report["target"]["code"], self.target.code)
        self.assertEqual(report["counts"]["source"]["submissions"], 1)
        source_submission.refresh_from_db()
        self.assertEqual(source_submission.problem, self.source)
        self.assertTrue(Problem.objects.filter(id=self.source.id).exists())

    def test_rejects_merge_from_smaller_id_to_larger_id_without_force(self):
        older = self.make_problem("older")
        newer = self.make_problem("newer")
        self.assertLess(older.id, newer.id)

        with self.assertRaisesRegex(Exception, "larger id into the smaller id"):
            ProblemMerge(older.code, newer.code, apply=False).run()

    def test_force_allows_merge_from_smaller_id_to_larger_id(self):
        older = self.make_problem("olderforce")
        newer = self.make_problem("newerforce")

        report = ProblemMerge(
            older.code,
            newer.code,
            apply=False,
            force=True,
        ).run()

        self.assertEqual(report["source"]["code"], older.code)
        self.assertEqual(report["target"]["code"], newer.code)

    def test_dry_run_reports_file_io(self):
        ProblemData.objects.create(
            problem=self.source,
            fileio_input="source.inp",
            fileio_output="source.out",
        )
        ProblemData.objects.create(
            problem=self.target,
            fileio_input="target.inp",
            fileio_output="target.out",
        )

        report = ProblemMerge(self.source.code, self.target.code, apply=False).run()

        self.assertEqual(report["file_io"]["source"]["input"], "source.inp")
        self.assertEqual(report["file_io"]["source"]["output"], "source.out")
        self.assertEqual(report["file_io"]["target"]["input"], "target.inp")
        self.assertEqual(report["file_io"]["target"]["output"], "target.out")

    def test_merge_moves_submissions_and_recalculates_best_submission(self):
        weak_target_submission = self.make_submission(self.target, points=20)
        strong_source_submission = self.make_submission(self.source, points=90)
        BestSubmission.objects.create(
            user=self.profile,
            problem=self.target,
            submission=weak_target_submission,
            points=20,
            case_total=100,
        )
        BestSubmission.objects.create(
            user=self.profile,
            problem=self.source,
            submission=strong_source_submission,
            points=90,
            case_total=100,
        )

        ProblemMerge(self.source.code, self.target.code, apply=True).run()

        strong_source_submission.refresh_from_db()
        self.assertEqual(strong_source_submission.problem, self.target)
        self.assertFalse(BestSubmission.objects.filter(problem=self.source).exists())
        best = BestSubmission.objects.get(user=self.profile, problem=self.target)
        self.assertEqual(best.submission, strong_source_submission)
        self.assertEqual(best.points, 90)
        self.assertFalse(Problem.objects.filter(id=self.source.id).exists())

    def test_merge_invalidates_duplicate_report_cache_after_commit(self):
        with patch(
            "judge.utils.problem_merge.update_duplicate_problem_report_cache_after_merge"
        ) as update_cache, patch(
            "judge.utils.problem_merge.prune_problem_embedding"
        ), patch(
            "judge.utils.problem_merge.index_problem_semantic_embedding"
        ):
            with self.captureOnCommitCallbacks(execute=True):
                ProblemMerge(self.source.code, self.target.code, apply=True).run()

        update_cache.assert_called_once_with(self.source.id, self.target.id)

    def test_create_pending_merge_blocks_reverse_duplicate_pair(self):
        create_pending_duplicate_problem_merge(self.source, self.target)

        with self.assertRaises(DuplicateProblemMergePending):
            create_pending_duplicate_problem_merge(
                self.target,
                self.source,
                force=True,
            )

    def test_cached_duplicate_candidates_hide_deleted_problem_rows(self):
        report = ProblemDuplicateReport.objects.create(
            status=ProblemDuplicateReport.SUCCESS,
        )
        ProblemDuplicateCandidate.objects.create(
            report=report,
            source_problem=None,
            target_problem=self.target,
            source_problem_id_snapshot=self.source.id,
            target_problem_id_snapshot=self.target.id,
            source_code=self.source.code,
            target_code=self.target.code,
            source_name=self.source.name,
            target_name=self.target.name,
            score=0.99,
        )

        self.assertEqual(get_cached_duplicate_problem_candidates(), [])

    def test_merge_repoints_contest_problem_without_target_conflict(self):
        contest = self.make_contest()
        source_cp = ContestProblem.objects.create(
            contest=contest, problem=self.source, points=100, order=1
        )
        submission = self.make_submission(self.source)
        participation = ContestParticipation.objects.create(
            contest=contest, user=self.profile
        )
        contest_submission = ContestSubmission.objects.create(
            submission=submission,
            problem=source_cp,
            participation=participation,
            points=100,
        )

        ProblemMerge(self.source.code, self.target.code, apply=True).run()

        source_cp.refresh_from_db()
        contest_submission.refresh_from_db()
        self.assertEqual(source_cp.problem, self.target)
        self.assertEqual(contest_submission.problem, source_cp)

    def test_merge_combines_contest_problem_when_target_already_in_contest(self):
        contest = self.make_contest()
        target_cp = ContestProblem.objects.create(
            contest=contest, problem=self.target, points=80, order=1
        )
        source_cp = ContestProblem.objects.create(
            contest=contest, problem=self.source, points=100, order=2
        )
        submission = self.make_submission(self.source)
        participation = ContestParticipation.objects.create(
            contest=contest, user=self.profile
        )
        contest_submission = ContestSubmission.objects.create(
            submission=submission,
            problem=source_cp,
            participation=participation,
            points=100,
        )

        ProblemMerge(self.source.code, self.target.code, apply=True).run()

        contest_submission.refresh_from_db()
        target_cp.refresh_from_db()
        self.assertEqual(contest_submission.problem, target_cp)
        self.assertFalse(ContestProblem.objects.filter(id=source_cp.id).exists())
        self.assertEqual(target_cp.points, 100)

    def test_merge_combines_course_lesson_problem_conflict(self):
        course = Course.objects.create(
            name="Merge Course", slug="merge-course", about="", is_open=True
        )
        lesson = CourseLesson.objects.create(
            course=course, title="Lesson", content="", order=1, points=100
        )
        target_lp = CourseLessonProblem.objects.create(
            lesson=lesson, problem=self.target, order=1, score=50
        )
        source_lp = CourseLessonProblem.objects.create(
            lesson=lesson, problem=self.source, order=2, score=80
        )

        ProblemMerge(self.source.code, self.target.code, apply=True).run()

        target_lp.refresh_from_db()
        self.assertFalse(CourseLessonProblem.objects.filter(id=source_lp.id).exists())
        self.assertEqual(target_lp.score, 80)
        self.assertEqual(target_lp.order, 1)

    def test_merge_unions_problem_metadata(self):
        problem_type = ProblemType.objects.create(name="merge", full_name="Merge")
        self.source.types.add(problem_type)
        self.source.authors.add(self.profile)
        self.source.allowed_languages.add(self.language)

        ProblemMerge(self.source.code, self.target.code, apply=True).run()

        self.assertIn(problem_type, self.target.types.all())
        self.assertIn(self.profile, self.target.authors.all())
        self.assertIn(self.language, self.target.allowed_languages.all())

    def test_merge_problem_owned_rows_keeps_target_conflicts(self):
        ProblemTranslation.objects.create(
            problem=self.source,
            language="vi",
            name="Source VI",
            description="Source statement",
        )
        target_translation = ProblemTranslation.objects.create(
            problem=self.target,
            language="vi",
            name="Target VI",
            description="Target statement",
        )
        source_limit = LanguageLimit.objects.create(
            problem=self.source,
            language=self.language,
            time_limit=2,
            memory_limit=32768,
        )
        source_template = LanguageTemplate.objects.create(
            problem=self.source,
            language=self.language,
            source="print('hello')",
        )
        source_solution_code = ProblemSolutionCode.objects.create(
            problem=self.source,
            language=self.language,
            name="Accepted",
            source_code="print(42)",
            expected_result="AC",
            order=7,
        )
        source_attachment = ProblemAttachment.objects.create(
            problem=self.source,
            file="merge/source.txt",
            description="source.txt",
            order=3,
        )
        source_solution = Solution.objects.create(
            problem=self.source,
            is_public=True,
            publish_on=timezone.now(),
            content="source editorial",
        )
        source_solution.authors.add(self.profile)

        report = ProblemMerge(self.source.code, self.target.code, apply=True).run()

        target_translation.refresh_from_db()
        self.assertEqual(target_translation.name, "Target VI")
        self.assertFalse(
            ProblemTranslation.objects.filter(problem=self.source).exists()
        )
        source_limit.refresh_from_db()
        source_template.refresh_from_db()
        source_solution_code.refresh_from_db()
        source_attachment.refresh_from_db()
        source_solution.refresh_from_db()
        self.assertEqual(source_limit.problem, self.target)
        self.assertEqual(source_template.problem, self.target)
        self.assertEqual(source_solution_code.problem, self.target)
        self.assertEqual(source_attachment.problem, self.target)
        self.assertEqual(source_solution.problem, self.target)
        self.assertIn("ProblemTranslation conflict", report["warnings"][0])

    def test_merge_moves_problem_data_if_target_has_none(self):
        source_data = ProblemData.objects.create(
            problem=self.source,
            generator_script="generate source",
        )
        source_case = ProblemTestCase.objects.create(
            dataset=self.source,
            order=1,
            is_pretest=False,
            points=100,
        )

        ProblemMerge(self.source.code, self.target.code, apply=True).run()

        source_data.refresh_from_db()
        source_case.refresh_from_db()
        self.assertEqual(source_data.problem, self.target)
        self.assertEqual(source_case.dataset, self.target)

    def test_merge_deletes_source_problem_data_if_target_already_has_data(self):
        source_data = ProblemData.objects.create(
            problem=self.source,
            generator_script="generate source",
        )
        target_data = ProblemData.objects.create(
            problem=self.target,
            generator_script="generate target",
        )
        source_case = ProblemTestCase.objects.create(
            dataset=self.source,
            order=1,
            is_pretest=False,
            points=100,
        )
        target_case = ProblemTestCase.objects.create(
            dataset=self.target,
            order=1,
            is_pretest=False,
            points=100,
        )

        ProblemMerge(self.source.code, self.target.code, apply=True).run()

        target_data.refresh_from_db()
        target_case.refresh_from_db()
        self.assertEqual(target_data.generator_script, "generate target")
        self.assertFalse(ProblemData.objects.filter(id=source_data.id).exists())
        self.assertFalse(ProblemTestCase.objects.filter(id=source_case.id).exists())
        self.assertEqual(target_case.dataset, self.target)

    def test_merge_moves_public_request_if_target_has_none(self):
        public_request = PublicRequest.objects.create(
            problem=self.source,
            requested_by=self.profile,
            status=PublicRequest.PENDING,
        )

        ProblemMerge(self.source.code, self.target.code, apply=True).run()

        public_request.refresh_from_db()
        self.assertEqual(public_request.problem, self.target)

    def test_merge_moves_generic_relations_and_combines_singletons(self):
        content_type = ContentType.objects.get_for_model(Problem)
        second_user = User.objects.create_user("merge_user_2", password="password")
        second_profile, _ = Profile.objects.get_or_create(
            user=second_user, defaults={"language": self.language}
        )
        source_comment = Comment.objects.create(
            author=self.profile,
            content_type=content_type,
            object_id=self.source.id,
            body="source comment",
        )
        source_ticket = Ticket.objects.create(
            title="source ticket",
            user=self.profile,
            content_type=content_type,
            object_id=self.source.id,
        )
        source_pagevote = PageVote.objects.create(
            content_type=content_type,
            object_id=self.source.id,
            score=1,
        )
        target_pagevote = PageVote.objects.create(
            content_type=content_type,
            object_id=self.target.id,
            score=-1,
        )
        PageVoteVoter.objects.create(
            voter=self.profile,
            pagevote=source_pagevote,
            score=1,
        )
        PageVoteVoter.objects.create(
            voter=second_profile,
            pagevote=target_pagevote,
            score=-1,
        )
        source_bookmark = BookMark.objects.create(
            content_type=content_type,
            object_id=self.source.id,
            score=1,
        )
        source_bookmark.users.add(self.profile)
        target_bookmark = BookMark.objects.create(
            content_type=content_type,
            object_id=self.target.id,
            score=1,
        )
        target_bookmark.users.add(second_profile)

        ProblemMerge(self.source.code, self.target.code, apply=True).run()

        source_comment.refresh_from_db()
        source_ticket.refresh_from_db()
        target_pagevote.refresh_from_db()
        target_bookmark.refresh_from_db()
        self.assertEqual(source_comment.object_id, self.target.id)
        self.assertEqual(source_ticket.object_id, self.target.id)
        self.assertFalse(PageVote.objects.filter(id=source_pagevote.id).exists())
        self.assertEqual(target_pagevote.score, 0)
        self.assertEqual(
            set(target_pagevote.votes.values_list("voter_id", flat=True)),
            {self.profile.id, second_profile.id},
        )
        self.assertFalse(BookMark.objects.filter(id=source_bookmark.id).exists())
        self.assertEqual(target_bookmark.score, 2)
        self.assertEqual(
            set(target_bookmark.users.values_list("id", flat=True)),
            {self.profile.id, second_profile.id},
        )
