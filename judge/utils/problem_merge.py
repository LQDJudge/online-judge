import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from judge.ml.problem_duplicates import (
    update_duplicate_problem_report_cache_after_merge,
)
from judge.ml.semantic_search import prune_problem_embedding
from judge.models import (
    BestSubmission,
    BookMark,
    Comment,
    ContestMoss,
    ContestProblem,
    ContestSubmission,
    CourseLessonProblem,
    LanguageLimit,
    LanguageTemplate,
    PageVote,
    PageVoteVoter,
    Problem,
    ProblemAttachment,
    ProblemData,
    ProblemPointsVote,
    ProblemSignatureGrader,
    ProblemSolutionCode,
    ProblemTestCase,
    ProblemTranslation,
    ProblemValidation,
    PublicRequest,
    Solution,
    Submission,
    Ticket,
)
from judge.tasks.semantic_search import index_problem_semantic_embedding

logger = logging.getLogger(__name__)

PROBLEM_MERGE_BATCH_SIZE = 1000


class ProblemMergeError(Exception):
    pass


@dataclass
class ProblemMerge:
    source_code: str
    target_code: str
    apply: bool = False
    force: bool = False
    report: dict = field(default_factory=dict)
    touched_contest_ids: set = field(default_factory=set)
    touched_course_ids: set = field(default_factory=set)
    affected_user_ids: set = field(default_factory=set)

    def run(self):
        with transaction.atomic():
            self.source = Problem.objects.select_for_update().get(code=self.source_code)
            self.target = Problem.objects.select_for_update().get(code=self.target_code)
            self._source_id = self.source.id
            self._target_id = self.target.id
            if self.source_id == self.target_id:
                raise ProblemMergeError("source and target must be different problems")
            if self.source_id < self.target_id and not self.force:
                raise ProblemMergeError(
                    "merge direction must move the larger id into the smaller id; "
                    "use --source %s --target %s or pass --force to override"
                    % (self.target.code, self.source.code)
                )

            self.report = self._build_report()
            if not self.apply:
                return self.report

            self._merge_m2m()
            self._merge_source_fields_when_target_blank()
            self._merge_singleton_problem_rows()
            self._merge_problem_test_cases()
            self._merge_problem_owned_rows()
            self._merge_problem_validations()
            self._merge_contest_problems()
            self._merge_course_lesson_problems()
            self._merge_submissions_and_best_submissions()
            self._merge_contest_moss()
            self._merge_problem_points_votes()
            self._merge_generic_problem_rows()
            self._delete_source()
            self._register_post_commit()

        self.report["applied"] = True
        return self.report

    @property
    def source_id(self):
        return self._source_id

    @property
    def target_id(self):
        return self._target_id

    def _build_report(self):
        source_counts = self._problem_counts(self.source)
        target_counts = self._problem_counts(self.target)
        contest_conflicts = list(
            ContestProblem.objects.filter(problem=self.source)
            .filter(
                contest_id__in=ContestProblem.objects.filter(
                    problem=self.target
                ).values("contest_id")
            )
            .values_list("contest__key", flat=True)
        )
        course_conflicts = list(
            CourseLessonProblem.objects.filter(problem=self.source)
            .filter(
                lesson_id__in=CourseLessonProblem.objects.filter(
                    problem=self.target
                ).values("lesson_id")
            )
            .values_list("lesson_id", flat=True)
        )
        return {
            "applied": False,
            "source": {
                "id": self.source.id,
                "code": self.source.code,
                "name": self.source.name,
            },
            "target": {
                "id": self.target.id,
                "code": self.target.code,
                "name": self.target.name,
            },
            "file_io": {
                "source": self._problem_file_io(self.source),
                "target": self._problem_file_io(self.target),
            },
            "counts": {
                "source": source_counts,
                "target": target_counts,
            },
            "conflicts": {
                "contest_keys": contest_conflicts,
                "course_lesson_ids": course_conflicts,
            },
        }

    def _problem_counts(self, problem):
        return {
            "submissions": Submission.objects.filter(problem=problem).count(),
            "best_submissions": BestSubmission.objects.filter(problem=problem).count(),
            "contest_problems": ContestProblem.objects.filter(problem=problem).count(),
            "course_lesson_problems": CourseLessonProblem.objects.filter(
                problem=problem
            ).count(),
            "translations": ProblemTranslation.objects.filter(problem=problem).count(),
            "language_limits": LanguageLimit.objects.filter(problem=problem).count(),
            "language_templates": LanguageTemplate.objects.filter(
                problem=problem
            ).count(),
            "attachments": ProblemAttachment.objects.filter(problem=problem).count(),
            "test_cases": ProblemTestCase.objects.filter(dataset=problem).count(),
            "validations": ProblemValidation.objects.filter(problem=problem).count(),
            "comments": self._generic_queryset(Comment, problem).count(),
            "tickets": self._generic_queryset(Ticket, problem).count(),
            "has_data": ProblemData.objects.filter(problem=problem).exists(),
            "has_public_request": PublicRequest.objects.filter(
                problem=problem
            ).exists(),
        }

    def _problem_file_io(self, problem):
        problem_data = ProblemData.objects.filter(problem=problem).first()
        if problem_data is None:
            return {
                "input": "",
                "output": "",
            }
        return {
            "input": problem_data.fileio_input or "",
            "output": problem_data.fileio_output or "",
        }

    def _merge_m2m(self):
        for field_name in (
            "authors",
            "curators",
            "testers",
            "types",
            "allowed_languages",
            "banned_users",
            "organizations",
        ):
            target_manager = getattr(self.target, field_name)
            source_manager = getattr(self.source, field_name)
            target_manager.add(*source_manager.all())
        self.target.judges.add(*self.source.judges.all())

    def _merge_source_fields_when_target_blank(self):
        changed_fields = []
        for field_name in ("summary", "description", "pdf_description", "og_image"):
            if not getattr(self.target, field_name) and getattr(
                self.source, field_name
            ):
                setattr(self.target, field_name, getattr(self.source, field_name))
                changed_fields.append(field_name)
        if changed_fields:
            self.target.save(update_fields=changed_fields)

    def _merge_singleton_problem_rows(self):
        self._merge_one_to_one_row(ProblemData)
        self._merge_one_to_one_row(PublicRequest)

    def _merge_problem_test_cases(self):
        if ProblemTestCase.objects.filter(dataset=self.target).exists():
            self._batched_delete(
                ProblemTestCase.objects.filter(dataset=self.source),
            )
            self.report.setdefault("warnings", []).append(
                "ProblemTestCase conflict; kept target"
            )
            return
        self._batched_update(
            ProblemTestCase.objects.filter(dataset=self.source),
            dataset=self.target,
        )

    def _merge_one_to_one_row(self, model):
        source_row = model.objects.filter(problem=self.source).first()
        if source_row is None:
            return
        if model.objects.filter(problem=self.target).exists():
            source_row.delete()
            self.report.setdefault("warnings", []).append(
                "%s conflict; kept target" % model.__name__
            )
            return
        source_row.problem = self.target
        source_row.save()

    def _merge_problem_owned_rows(self):
        self._merge_unique_by_language(ProblemTranslation, ["name", "description"])
        self._merge_unique_by_language(LanguageLimit, ["time_limit", "memory_limit"])
        self._merge_unique_by_language(LanguageTemplate, ["source"])
        self._merge_signature_graders()
        self._merge_solution_codes()
        self._merge_attachments()
        self._merge_solution()

    def _merge_problem_validations(self):
        self._batched_update(
            ProblemValidation.objects.filter(problem=self.source),
            problem=self.target,
        )

    def _merge_unique_by_language(self, model, compare_fields):
        for source_row in list(model.objects.filter(problem=self.source)):
            target_row = model.objects.filter(
                problem=self.target,
                language=source_row.language,
            ).first()
            if target_row is None:
                source_row.problem = self.target
                source_row.save()
                continue

            differs = any(
                getattr(source_row, field_name) != getattr(target_row, field_name)
                for field_name in compare_fields
            )
            if differs and not self.force:
                self.report.setdefault("warnings", []).append(
                    "%s conflict for language %s; kept target"
                    % (model.__name__, source_row.language)
                )
            source_row.delete()

    def _merge_signature_graders(self):
        for source_row in list(
            ProblemSignatureGrader.objects.filter(problem=self.source)
        ):
            target_exists = ProblemSignatureGrader.objects.filter(
                problem=self.target,
                language=source_row.language,
            ).exists()
            if target_exists:
                source_row.delete()
            else:
                source_row.problem = self.target
                source_row.save()

    def _merge_solution_codes(self):
        target_hashes = set(
            ProblemSolutionCode.objects.filter(problem=self.target).values_list(
                "language_id", "source_code"
            )
        )
        max_order = (
            ProblemSolutionCode.objects.filter(problem=self.target)
            .order_by("-order")
            .values_list("order", flat=True)
            .first()
            or 0
        )
        for source_row in list(ProblemSolutionCode.objects.filter(problem=self.source)):
            key = (source_row.language_id, source_row.source_code)
            if key in target_hashes:
                source_row.delete()
                continue
            max_order += 1
            source_row.problem = self.target
            source_row.order = max_order
            source_row.save()

    def _merge_attachments(self):
        max_order = (
            ProblemAttachment.objects.filter(problem=self.target)
            .order_by("-order")
            .values_list("order", flat=True)
            .first()
            or 0
        )
        target_files = set(
            ProblemAttachment.objects.filter(problem=self.target).values_list(
                "file", flat=True
            )
        )
        for attachment in list(ProblemAttachment.objects.filter(problem=self.source)):
            if attachment.file.name in target_files:
                attachment.delete()
                continue
            max_order += 1
            attachment.problem = self.target
            attachment.order = max_order
            attachment.save()

    def _merge_solution(self):
        source_solution = Solution.objects.filter(problem=self.source).first()
        if not source_solution:
            return
        target_solution = Solution.objects.filter(problem=self.target).first()
        if target_solution:
            target_solution.authors.add(*source_solution.authors.all())
            source_solution.delete()
        else:
            source_solution.problem = self.target
            source_solution.save()

    def _merge_contest_problems(self):
        for source_cp in list(ContestProblem.objects.filter(problem=self.source)):
            self.touched_contest_ids.add(source_cp.contest_id)
            target_cp = ContestProblem.objects.filter(
                contest=source_cp.contest,
                problem=self.target,
            ).first()
            if target_cp is None:
                source_cp.problem = self.target
                source_cp.save()
                continue

            self._batched_update(
                ContestSubmission.objects.filter(problem=source_cp),
                problem=target_cp,
            )
            self._merge_contest_problem_settings(source_cp, target_cp)
            source_cp.delete()

    def _merge_contest_problem_settings(self, source_cp, target_cp):
        update_fields = []
        if source_cp.points > target_cp.points:
            target_cp.points = source_cp.points
            update_fields.append("points")
        if source_cp.partial and not target_cp.partial:
            target_cp.partial = True
            update_fields.append("partial")
        if source_cp.show_testcases and not target_cp.show_testcases:
            target_cp.show_testcases = True
            update_fields.append("show_testcases")
        if target_cp.max_submissions != 0 and (
            source_cp.max_submissions == 0
            or source_cp.max_submissions > target_cp.max_submissions
        ):
            target_cp.max_submissions = source_cp.max_submissions
            update_fields.append("max_submissions")
        if update_fields:
            target_cp.save(update_fields=update_fields)

    def _merge_course_lesson_problems(self):
        for source_lp in list(CourseLessonProblem.objects.filter(problem=self.source)):
            self.touched_course_ids.add(source_lp.lesson.course_id)
            target_lp = CourseLessonProblem.objects.filter(
                lesson=source_lp.lesson,
                problem=self.target,
            ).first()
            if target_lp is None:
                source_lp.problem = self.target
                source_lp.save()
                continue

            update_fields = []
            if source_lp.score > target_lp.score:
                target_lp.score = source_lp.score
                update_fields.append("score")
            if source_lp.order < target_lp.order:
                target_lp.order = source_lp.order
                update_fields.append("order")
            if update_fields:
                target_lp.save(update_fields=update_fields)
            source_lp.delete()

    def _merge_submissions_and_best_submissions(self):
        self.affected_user_ids = set(
            Submission.objects.filter(
                problem__in=[self.source, self.target]
            ).values_list("user_id", flat=True)
        )
        self._batched_update(
            Submission.objects.filter(problem=self.source),
            problem=self.target,
        )
        self._batched_delete(
            BestSubmission.objects.filter(
                problem__in=[self.source, self.target],
                user_id__in=self.affected_user_ids,
            )
        )
        for user_id in self.affected_user_ids:
            BestSubmission.recalculate_for_user_problem(user_id, self.target_id)

    def _merge_contest_moss(self):
        for source_row in list(ContestMoss.objects.filter(problem=self.source)):
            existing = ContestMoss.objects.filter(
                contest=source_row.contest,
                problem=self.target,
                language=source_row.language,
            ).first()
            if existing:
                existing.submission_count = max(
                    existing.submission_count,
                    source_row.submission_count,
                )
                if not existing.url and source_row.url:
                    existing.url = source_row.url
                existing.save()
                source_row.delete()
            else:
                source_row.problem = self.target
                source_row.save()

    def _merge_problem_points_votes(self):
        self._batched_update(
            ProblemPointsVote.objects.filter(problem=self.source),
            problem=self.target,
        )

    def _merge_generic_problem_rows(self):
        self._batched_update(
            self._generic_queryset(Comment, self.source),
            object_id=self.target_id,
        )
        self._batched_update(
            self._generic_queryset(Ticket, self.source),
            object_id=self.target_id,
        )
        self._merge_pagevotes()
        self._merge_bookmarks()

    def _merge_pagevotes(self):
        source_pagevote = self._generic_queryset(PageVote, self.source).first()
        if source_pagevote is None:
            return
        target_pagevote = self._generic_queryset(PageVote, self.target).first()
        if target_pagevote is None:
            source_pagevote.object_id = self.target_id
            source_pagevote.save(update_fields=["object_id"])
            return

        target_pagevote.score += source_pagevote.score
        target_pagevote.save(update_fields=["score"])
        for source_vote in list(source_pagevote.votes.all()):
            target_vote = PageVoteVoter.objects.filter(
                voter=source_vote.voter,
                pagevote=target_pagevote,
            ).first()
            if target_vote is None:
                source_vote.pagevote = target_pagevote
                source_vote.save(update_fields=["pagevote"])
            else:
                target_vote.score += source_vote.score
                target_vote.save(update_fields=["score"])
                source_vote.delete()
        source_pagevote.delete()

    def _merge_bookmarks(self):
        source_bookmark = self._generic_queryset(BookMark, self.source).first()
        if source_bookmark is None:
            return
        target_bookmark = self._generic_queryset(BookMark, self.target).first()
        if target_bookmark is None:
            source_bookmark.object_id = self.target_id
            source_bookmark.save(update_fields=["object_id"])
            return

        target_bookmark.users.add(*source_bookmark.users.all())
        target_bookmark.score = target_bookmark.users.count()
        target_bookmark.save(update_fields=["score"])
        source_bookmark.delete()

    def _generic_queryset(self, model, problem):
        content_type = ContentType.objects.get_for_model(Problem)
        return model.objects.filter(
            content_type=content_type,
            object_id=problem.id,
        )

    def _batched_update(self, queryset, **fields):
        model = queryset.model
        total = 0
        while True:
            ids = self._next_batch_ids(queryset)
            if not ids:
                return total
            total += model.objects.filter(pk__in=ids).update(**fields)

    def _batched_delete(self, queryset):
        model = queryset.model
        total = 0
        while True:
            ids = self._next_batch_ids(queryset)
            if not ids:
                return total
            deleted, _ = model.objects.filter(pk__in=ids).delete()
            total += deleted

    def _next_batch_ids(self, queryset):
        return list(
            queryset.order_by("pk").values_list("pk", flat=True)[
                :PROBLEM_MERGE_BATCH_SIZE
            ]
        )

    def _delete_source(self):
        self.source.delete()

    def _register_post_commit(self):
        def post_commit():
            update_duplicate_problem_report_cache_after_merge(
                self.source_id,
                self.target_id,
            )
            if getattr(settings, "USE_ML", False):
                try:
                    prune_problem_embedding(self.source_id)
                    index_problem_semantic_embedding.delay(self.target_id, force=True)
                except Exception:
                    logger.exception(
                        "Failed to refresh semantic index after merging problem %s into %s",
                        self.source_id,
                        self.target_id,
                    )

        transaction.on_commit(post_commit)
