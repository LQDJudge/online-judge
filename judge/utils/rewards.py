"""Persistent course awards and cached presentation data for avatar frames."""

from collections import defaultdict
from importlib import import_module
from itertools import islice

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils.translation import gettext as _

from judge.caching import cache_wrapper
from judge.models import (
    AvatarFrame,
    BestQuizAttempt,
    ContestParticipation,
    ContestProblem,
    CourseContest,
    CourseLesson,
    CourseLessonProblem,
    CourseLessonQuiz,
    CourseRole,
    ProfileFrame,
)

DEFAULT_COURSE_FRAMES = {
    "course-iron": "cp-sat",
    "course-bronze": "cp-dong",
    "course-silver": "cp-bac",
    "course-gold": "cp-vang",
    "cses": "cses",
}


def _profile_id(profile):
    return profile.pk if hasattr(profile, "pk") else profile


def _equipped_frames_batch(args_list):
    profile_ids = [_profile_id(args[0]) for args in args_list]
    rows = (
        ProfileFrame.objects.filter(
            profile_id__in=profile_ids, is_equipped=True, frame__is_active=True
        )
        .select_related("frame")
        .order_by("id")
    )
    result = {}
    for ownership in rows:
        frame = ownership.frame
        result[ownership.profile_id] = {
            "id": frame.pk,
            "slug": frame.slug,
            "name": frame.name,
            "style": frame.style,
            "label": frame.label,
            "artwork_url": frame.artwork_url,
            "artwork_scale": frame.artwork_scale,
            "avatar_center_x": float(frame.avatar_center_x),
            "avatar_center_y": float(frame.avatar_center_y),
        }
    return [result.get(profile_id) for profile_id in profile_ids]


@cache_wrapper(
    prefix="rewardEquipped2",
    timeout=3600,
    batch_fn=_equipped_frames_batch,
    transaction_sensitive=True,
)
def get_equipped_frame(profile):
    return _equipped_frames_batch([(profile,)])[0]


def invalidate_equipped_frame(profile):
    profile_id = _profile_id(profile)
    get_equipped_frame.dirty(profile_id)
    transaction.on_commit(lambda: get_equipped_frame.dirty(profile_id))


@transaction.atomic
def grant_manual_frame(frame, profiles, *, granted_by, reason, batch_size=500):
    """Grant to a selected queryset without changing existing awards or equipment."""
    if not granted_by.has_perm("judge.add_profileframe"):
        raise PermissionDenied
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    frame = AvatarFrame.objects.select_for_update().get(pk=frame.pk)
    if not frame.is_active or frame.unlock_type != AvatarFrame.UnlockType.MANUAL:
        raise ValidationError(_("Only active manual frames can be granted."))
    reason = reason.strip()
    if not reason:
        raise ValidationError(_("Enter a reason for this grant."))
    profile_ids = (
        profiles.order_by("pk")
        .values_list("pk", flat=True)
        .iterator(chunk_size=batch_size)
    )
    processed = 0
    while True:
        batch = list(islice(profile_ids, batch_size))
        if not batch:
            break
        existing = set(
            ProfileFrame.objects.filter(frame=frame, profile_id__in=batch).values_list(
                "profile_id", flat=True
            )
        )
        # New ownership is unequipped; no cached equipped frame changes here.
        ProfileFrame.objects.bulk_create(
            [
                ProfileFrame(
                    profile_id=profile_id,
                    frame=frame,
                    granted_by=granted_by,
                    reason=reason,
                )
                for profile_id in batch
                if profile_id not in existing
            ],
            ignore_conflicts=True,
            batch_size=batch_size,
        )
        processed += len(batch)
    return processed


def sync_course_rewards(profile):
    """Award newly completed courses; awards survive subsequent curriculum edits."""
    return sync_course_rewards_batch([profile]).get(profile.pk, {})


def sync_course_rewards_batch(profiles):
    """Compute a bounded batch with shared lesson, quiz and contest queries.

    Public awards must never reveal hidden grades, even for course editors.
    Full completion is checked against every positively weighted component,
    using unrounded scores. Empty or unmapped courses cannot award frames.
    """
    profiles = list(profiles)
    if not profiles:
        return {}
    # Defer the view import until runtime to avoid the view/model startup cycle.
    grades = import_module("judge.views.course")
    frames = list(
        AvatarFrame.objects.filter(
            is_active=True,
            unlock_type=AvatarFrame.UnlockType.COURSE,
        )
    )
    course_ids = {frame.course_id for frame in frames if frame.course_id}
    enrolled = set(
        CourseRole.objects.filter(
            course_id__in=course_ids, user__in=profiles
        ).values_list("user_id", "course_id")
    )
    lessons = list(
        CourseLesson.objects.filter(course_id__in=course_ids, is_visible=True)
    )
    lesson_ids = [lesson.pk for lesson in lessons]
    lesson_problems = defaultdict(list)
    problem_ids = set()
    for row in CourseLessonProblem.objects.filter(lesson_id__in=lesson_ids).values(
        "lesson_id", "problem_id", "score"
    ):
        lesson_problems[row["lesson_id"]].append(row)
        problem_ids.add(row["problem_id"])
    lesson_quizzes = defaultdict(list)
    for row in CourseLessonQuiz.objects.filter(
        lesson_id__in=lesson_ids, is_visible=True
    ).values("id", "lesson_id", "points", "is_result_hidden"):
        lesson_quizzes[row["lesson_id"]].append(row)
    visible_quiz_ids = [
        quiz["id"]
        for quizzes in lesson_quizzes.values()
        for quiz in quizzes
        if not quiz["is_result_hidden"]
    ]
    best_quiz_scores = {}
    for row in BestQuizAttempt.objects.filter(
        user__in=profiles,
        lesson_quiz_id__in=visible_quiz_ids,
    ).values("user_id", "lesson_quiz_id", "score", "max_score"):
        score, total = row["score"], row["max_score"]
        best_quiz_scores[(row["user_id"], row["lesson_quiz_id"])] = {
            "ratio": float(min(max(score / total, 0), 1)) if total else 0,
            "complete": total > 0 and score >= total,
        }
    progress_data = {
        "lesson_problems": lesson_problems,
        "lesson_quizzes": lesson_quizzes,
        "best_quiz_scores": best_quiz_scores,
    }
    problem_scores = grades.bulk_max_case_points_per_problem(
        profiles, problem_ids, viewer=AnonymousUser()
    )
    course_contests = list(
        CourseContest.objects.filter(
            course_id__in=course_ids, contest__is_visible=True
        ).select_related("contest")
    )
    contest_ids = [item.contest_id for item in course_contests]
    hidden_contests = set(
        ContestProblem.objects.filter(
            contest_id__in=contest_ids,
            is_result_hidden=True,
            points__gt=0,
        ).values_list("contest_id", flat=True)
    )
    contest_points = dict(
        ContestProblem.objects.filter(
            contest_id__in=contest_ids,
        )
        .values("contest_id")
        .annotate(total=Sum("points"))
        .values_list("contest_id", "total")
    )
    participation_scores = {
        (row["contest_id"], row["user_id"]): row["score"]
        for row in ContestParticipation.objects.filter(
            contest_id__in=contest_ids,
            user__in=profiles,
            virtual=0,
            is_disqualified=False,
        )
        .exclude(contest_id__in=hidden_contests)
        .values("contest_id", "user_id", "score")
    }
    contests_data = {
        "contest_points": contest_points,
        "participation_scores": participation_scores,
    }
    lessons_by_course = defaultdict(list)
    contests_by_course = defaultdict(list)
    for lesson in lessons:
        lessons_by_course[lesson.course_id].append(lesson)
    for contest in course_contests:
        contests_by_course[contest.course_id].append(contest)
    existing = set(
        ProfileFrame.objects.filter(profile__in=profiles, frame__in=frames).values_list(
            "profile_id", "frame_id"
        )
    )
    result = {profile.pk: {} for profile in profiles}
    new_awards = []
    for course_id in course_ids:
        course_lessons = lessons_by_course[course_id]
        contests = contests_by_course[course_id]
        lesson_grades = grades.bulk_calculate_lessons_progress(
            profiles,
            course_lessons,
            problem_scores,
            progress_data=progress_data,
        )
        contest_grades = grades.bulk_calculate_contests_progress(
            profiles,
            contests,
            progress_data=contests_data,
        )
        for profile in profiles:
            summary = grades.calculate_total_progress(
                lesson_grades[profile], contest_grades[profile]
            )
            complete = summary["total_points"] > 0
            # Comparing each raw score avoids rounding 99.999... to full marks.
            for lesson in course_lessons:
                if not lesson.points:
                    continue
                if lesson_grades[profile][lesson.pk]["total_points"] <= 0:
                    complete = False
                for problem in lesson_problems[lesson.pk]:
                    if not problem["score"]:
                        continue
                    score = problem_scores.get(profile.pk, {}).get(
                        problem["problem_id"], {}
                    )
                    if (
                        not score.get("case_total")
                        or score.get("case_points", 0) < score["case_total"]
                    ):
                        complete = False
                for quiz in lesson_quizzes[lesson.pk]:
                    if quiz["points"] and not best_quiz_scores.get(
                        (profile.pk, quiz["id"]), {}
                    ).get("complete"):
                        complete = False
            for contest in contests:
                if contest.points:
                    total = contest_points.get(contest.contest_id, 0) or 0
                    score = participation_scores.get(
                        (contest.contest_id, profile.pk), 0
                    )
                    if total <= 0 or score < total:
                        complete = False
            is_enrolled = (profile.pk, course_id) in enrolled
            for frame in frames:
                if frame.course_id != course_id:
                    continue
                progress = dict(
                    summary,
                    achieved=summary["achieved_points"],
                    total=summary["total_points"],
                    enrolled=is_enrolled,
                    eligible=is_enrolled,
                    complete=complete and is_enrolled,
                )
                result[profile.pk][frame.pk] = progress
                if progress["complete"] and (profile.pk, frame.pk) not in existing:
                    new_awards.append(
                        ProfileFrame(
                            profile_id=profile.pk,
                            frame=frame,
                            reason="Completed 100% of course points.",
                        )
                    )
    for profile in profiles:
        for frame in frames:
            result[profile.pk].setdefault(
                frame.pk,
                {
                    "achieved_points": 0,
                    "total_points": 0,
                    "achieved": 0,
                    "total": 0,
                    "percentage": 0,
                    "enrolled": False,
                    "eligible": False,
                    "complete": False,
                },
            )
    if new_awards:
        # Concurrent refreshes converge on the unique (profile, frame) record.
        ProfileFrame.objects.bulk_create(new_awards, ignore_conflicts=True)
    return result
