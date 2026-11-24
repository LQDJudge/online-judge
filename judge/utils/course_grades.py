"""Bulk course scores, with separate internal and viewer-visible calculations."""

from django.db.models import Q, Sum

from judge.models import (
    BestSubmission,
    Submission,
    CourseLesson,
    CourseLessonQuiz,
    QuizQuestionAssignment,
    BestQuizAttempt,
    Contest,
    ContestParticipation,
    ContestProblem,
    Course,
    Quiz,
)
from judge.utils.hidden_results import (
    exclude_hidden_result_submissions,
    hidden_result_submission_ids,
)


def _set_problem_points(
    result, user_id, problem_id, case_points, case_total, is_result_hidden=False
):
    case_total = case_total or 0
    case_points = case_points or 0
    if case_total > 0:
        case_points = min(max(case_points, 0), case_total)
    else:
        case_points = 0
    result.setdefault(user_id, {})[problem_id] = {
        "case_points": case_points,
        "case_total": case_total,
        "is_result_hidden": is_result_hidden,
    }


def bulk_max_case_points_per_problem(students, all_problems, viewer=None):
    """
    Fetch max case points for all students and problems using BestSubmission cache.
    Returns nested dict for O(1) lookups: {user_id: {problem_id: {case_points, case_total}}}

    This uses the BestSubmission table which caches the best submission per user/problem.
    When viewer is provided, hidden contest-result best submissions are excluded from
    the displayed score and replaced with the best visible submission for that pair.
    """
    if not students or not all_problems:
        return {}

    student_ids = [
        student.id if hasattr(student, "id") else student for student in students
    ]
    problem_ids = [
        problem.id if hasattr(problem, "id") else problem for problem in all_problems
    ]
    result = {}

    # Try to use BestSubmission cache first
    best_subs_qs = BestSubmission.objects.filter(
        user_id__in=student_ids, problem_id__in=problem_ids, case_total__gt=0
    )

    best_subs = list(
        best_subs_qs.values(
            "user_id", "problem_id", "points", "case_total", "submission_id"
        )
    )

    hidden_pairs = set()
    if viewer is not None:
        hidden_pairs = set(
            best_subs_qs.filter(
                submission_id__in=hidden_result_submission_ids(viewer)
            ).values_list("user_id", "problem_id")
        )

    for best_sub in best_subs:
        user_id = best_sub["user_id"]
        problem_id = best_sub["problem_id"]
        if viewer is not None and (user_id, problem_id) in hidden_pairs:
            continue

        _set_problem_points(
            result,
            user_id,
            problem_id,
            best_sub["points"],
            best_sub["case_total"],
        )

    if not hidden_pairs:
        return result

    hidden_user_ids = {user_id for user_id, _problem_id in hidden_pairs}
    hidden_problem_ids = {problem_id for _user_id, problem_id in hidden_pairs}

    fallback_submissions = exclude_hidden_result_submissions(
        Submission.objects.filter(
            user_id__in=hidden_user_ids,
            problem_id__in=hidden_problem_ids,
            status="D",
            case_total__gt=0,
        ),
        viewer,
    ).order_by("user_id", "problem_id", "-points", "-date")

    for submission in fallback_submissions.values(
        "user_id", "problem_id", "case_points", "case_total"
    ):
        pair = (submission["user_id"], submission["problem_id"])
        if pair not in hidden_pairs:
            continue
        user_id, problem_id = pair
        if problem_id in result.get(user_id, {}):
            continue
        _set_problem_points(
            result,
            user_id,
            problem_id,
            submission["case_points"],
            submission["case_total"],
        )

    for user_id, problem_id in hidden_pairs:
        if problem_id not in result.get(user_id, {}):
            _set_problem_points(
                result, user_id, problem_id, 0, 0, is_result_hidden=True
            )

    return result


def _bulk_quiz_total_points(quiz_ids):
    if not quiz_ids:
        return {}
    return {
        row["quiz_id"]: row["total"] or 0
        for row in QuizQuestionAssignment.objects.filter(quiz_id__in=set(quiz_ids))
        .values("quiz_id")
        .annotate(total=Sum("points"))
    }


def hidden_lesson_quiz_ids(rows, course, viewer):
    """Batch the same course-staff/quiz-editor exemption used by quiz results."""
    if (
        viewer is None
        or viewer.is_superuser
        or Course.is_editable_by(course, getattr(viewer, "profile", None))
    ):
        return set()
    flagged = [row for row in rows if row["is_result_hidden"]]
    if not flagged:
        return set()
    editable = set(
        Quiz.objects.filter(pk__in=[row["quiz_id"] for row in flagged])
        .filter(Q(authors=viewer.profile) | Q(curators=viewer.profile))
        .values_list("pk", flat=True)
    )
    return {row["id"] for row in flagged if row["quiz_id"] not in editable}


def _prepare_lesson_progress_data(students, lessons, viewer=None):
    lessons = list(lessons)
    problem_rows = CourseLesson.get_problems_and_scores.batch(
        [(lesson,) for lesson in lessons]
    )
    lesson_problems = {lesson.id: rows for lesson, rows in zip(lessons, problem_rows)}
    lesson_quizzes = {lesson.id: [] for lesson in lessons}
    lesson_ids = [lesson.id for lesson in lessons]
    all_lesson_quizzes = (
        CourseLessonQuiz.objects.filter(lesson_id__in=lesson_ids, is_visible=True)
        .order_by("lesson_id", "order", "id")
        .values("id", "lesson_id", "quiz_id", "points", "is_result_hidden")
    )
    quiz_totals = _bulk_quiz_total_points(
        {item["quiz_id"] for item in all_lesson_quizzes}
    )
    hidden_ids = (
        hidden_lesson_quiz_ids(all_lesson_quizzes, lessons[0].course, viewer)
        if lessons
        else set()
    )
    for item in all_lesson_quizzes:
        lesson_quizzes[item["lesson_id"]].append(
            {
                "id": item["id"],
                "quiz_id": item["quiz_id"],
                "points": item["points"],
                "is_result_hidden": item["id"] in hidden_ids,
                "max_score": quiz_totals.get(item["quiz_id"], 0),
            }
        )

    lesson_quiz_ids = [
        quiz["id"]
        for quizzes in lesson_quizzes.values()
        for quiz in quizzes
        if not quiz["is_result_hidden"]
    ]
    student_ids = [student.id for student in students]
    best_quiz_scores = {}
    if lesson_quiz_ids and student_ids:
        attempts = BestQuizAttempt.objects.filter(
            user_id__in=student_ids,
            lesson_quiz_id__in=lesson_quiz_ids,
        ).values("user_id", "lesson_quiz_id", "score", "max_score")
        for attempt in attempts.iterator(chunk_size=2000):
            score = float(attempt["score"] or 0)
            max_score = float(attempt["max_score"] or 0)
            best_quiz_scores[(attempt["user_id"], attempt["lesson_quiz_id"])] = {
                "score": score,
                "max_score": max_score,
                "ratio": (min(max(score / float(max_score), 0), 1) if max_score else 0),
            }

    return {
        "lesson_problems": lesson_problems,
        "lesson_quizzes": lesson_quizzes,
        "best_quiz_scores": best_quiz_scores,
    }


def _calculate_lesson_summary(
    student_id, lesson_id, bulk_problem_points, progress_data
):
    achieved_points = total_points = 0
    is_result_hidden = False
    student_points = bulk_problem_points.get(student_id, {})

    for problem in progress_data["lesson_problems"][lesson_id]:
        problem_data = student_points.get(problem["problem_id"])
        is_result_hidden |= bool(problem_data and problem_data.get("is_result_hidden"))
        if problem_data and problem_data["case_total"]:
            achieved_points += (
                problem_data["case_points"]
                / problem_data["case_total"]
                * problem["score"]
            )
        total_points += problem["score"]

    for quiz in progress_data["lesson_quizzes"][lesson_id]:
        is_result_hidden |= quiz.get("is_result_hidden", False)
        quiz_points = quiz["points"] or 0
        total_points += quiz_points
        attempt = progress_data["best_quiz_scores"].get((student_id, quiz["id"]))
        achieved_points += (attempt["ratio"] if attempt else 0) * quiz_points

    return {
        "is_result_hidden": is_result_hidden,
        "achieved_points": achieved_points,
        "total_points": total_points,
        "percentage": achieved_points / total_points * 100 if total_points else 0,
    }


def _build_lesson_grade_details(students, lesson, bulk_problem_points, progress_data):
    grades = {}
    lesson_id = lesson.id
    for student in students:
        student_points = bulk_problem_points.get(student.id, {})
        student_grade = dict(student_points)
        student_grade["total"] = _calculate_lesson_summary(
            student.id, lesson_id, bulk_problem_points, progress_data
        )

        for quiz in progress_data["lesson_quizzes"][lesson_id]:
            attempt = progress_data["best_quiz_scores"].get((student.id, quiz["id"]))
            ratio = attempt["ratio"] if attempt else 0
            student_grade[f"quiz_{quiz['id']}"] = {
                "is_result_hidden": quiz.get("is_result_hidden", False),
                "score": attempt["score"] if attempt else 0,
                "max_score": attempt["max_score"] if attempt else quiz["max_score"],
                "achieved": ratio * (quiz["points"] or 0),
            }

        grades[student] = student_grade
    return grades


def bulk_calculate_lessons_progress(
    students,
    lessons,
    bulk_problem_points,
    *,
    progress_data=None,
    include_details=True,
    viewer=None,
):
    """
    Calculate progress for all students and lessons using pre-fetched data.
    Includes both problem scores and quiz scores.

    Args:
        students: List of Profile objects
        lessons: QuerySet of CourseLesson objects (should be prefetched)
        bulk_problem_points: Dict from bulk_max_case_points_per_problem()
        progress_data: Optional prepared lookup from _prepare_lesson_progress_data()
        include_details: Include per-lesson rows instead of totals only

    Returns:
        Dict: {student: lesson_progress_dict}
    """
    lessons = list(lessons)
    if progress_data is None:
        progress_data = _prepare_lesson_progress_data(students, lessons, viewer=viewer)

    results = {}
    for student in students:
        student_results = {}
        total_achieved_points = total_lesson_points = 0
        is_result_hidden = False

        for lesson in lessons:
            summary = _calculate_lesson_summary(
                student.id, lesson.id, bulk_problem_points, progress_data
            )
            is_result_hidden |= summary.get("is_result_hidden", False)
            if include_details:
                student_results[lesson.id] = summary

            if summary["total_points"]:
                total_achieved_points += (
                    summary["achieved_points"] / summary["total_points"] * lesson.points
                )
            total_lesson_points += lesson.points

        student_results["total"] = {
            "is_result_hidden": is_result_hidden,
            "achieved_points": total_achieved_points,
            "total_points": total_lesson_points,
            "percentage": (
                total_achieved_points / total_lesson_points * 100
                if total_lesson_points
                else 0
            ),
        }
        results[student] = student_results

    return results


def _prepare_contest_progress_data(students, course_contests, viewer=None):
    contests = [course_contest.contest for course_contest in course_contests]
    hidden_contest_ids = set()
    if viewer is not None:
        hidden_ids = set(
            ContestProblem.objects.filter(
                contest__in=contests, is_result_hidden=True
            ).values_list("contest_id", flat=True)
        )
        if hidden_ids and not (
            viewer.is_authenticated
            and (viewer.is_superuser or viewer.has_perm("judge.edit_all_contest"))
        ):
            editable = (
                set(
                    Contest.objects.filter(pk__in=hidden_ids)
                    .filter(Q(authors=viewer.profile) | Q(curators=viewer.profile))
                    .values_list("pk", flat=True)
                )
                if viewer.is_authenticated
                else set()
            )
            hidden_contest_ids = hidden_ids - editable
    participations = (
        ContestParticipation.objects.exclude(contest_id__in=hidden_contest_ids)
        .filter(contest__in=contests, user__in=students, virtual=0)
        .values("contest", "user", "score")
    )
    participation_scores = {
        (row["contest"], row["user"]): row["score"] for row in participations
    }
    contest_totals = (
        ContestProblem.objects.filter(contest__in=contests)
        .values("contest")
        .annotate(total_points=Sum("points"))
    )
    contest_points = {
        row["contest"]: row["total_points"] or 0 for row in contest_totals
    }
    return {
        "hidden_contest_ids": hidden_contest_ids,
        "participation_scores": participation_scores,
        "contest_points": contest_points,
    }


def bulk_calculate_contests_progress(
    students, course_contests, *, progress_data=None, include_details=True, viewer=None
):
    """
    Calculate contest progress for all students using bulk queries.
    Returns nested dict: {student: contest_progress_dict}
    """
    course_contests = list(course_contests)
    if progress_data is None:
        progress_data = _prepare_contest_progress_data(
            students, course_contests, viewer=viewer
        )

    results = {}
    for student in students:
        student_results = {}
        total_achieved_points = total_contest_points = 0

        for course_contest in course_contests:
            contest = course_contest.contest
            achieved_points = progress_data["participation_scores"].get(
                (contest.id, student.id), 0
            )
            total_points = progress_data["contest_points"].get(contest.id, 0)

            if include_details:
                student_results[course_contest.id] = {
                    "is_result_hidden": contest.pk
                    in progress_data.get("hidden_contest_ids", set()),
                    "achieved_points": achieved_points,
                    "total_points": total_points,
                    "percentage": (
                        min(max(achieved_points / total_points, 0), 1) * 100
                        if total_points
                        else 0
                    ),
                }

            if total_points:
                total_achieved_points += (
                    min(max(achieved_points / total_points, 0), 1)
                    * course_contest.points
                )
            total_contest_points += course_contest.points

        student_results["total"] = {
            "is_result_hidden": bool(progress_data.get("hidden_contest_ids")),
            "achieved_points": total_achieved_points,
            "total_points": total_contest_points,
            "percentage": (
                total_achieved_points / total_contest_points * 100
                if total_contest_points
                else 0
            ),
        }
        results[student] = student_results

    return results


def calculate_total_progress(lesson_progress, contest_progress):
    lesson_total = lesson_progress["total"]
    contest_total = contest_progress["total"]
    total_achieved_points = (
        lesson_total["achieved_points"] + contest_total["achieved_points"]
    )
    total_points = lesson_total["total_points"] + contest_total["total_points"]

    res = {
        "is_result_hidden": lesson_total.get("is_result_hidden", False)
        or contest_total.get("is_result_hidden", False),
        "achieved_points": total_achieved_points,
        "total_points": total_points,
        "percentage": total_achieved_points / total_points * 100 if total_points else 0,
    }
    return res
