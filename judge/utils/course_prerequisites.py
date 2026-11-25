"""
Course Lesson Prerequisites Utility Module

This module provides functions for managing the prerequisites system for course lessons.
It includes the unlock algorithm that uses BFS to propagate unlock states based on
user grades and prerequisite requirements.
"""

import json
from collections import defaultdict, deque

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.utils.translation import gettext as _

from judge.models import (
    Course,
    CourseLessonProgress,
    CourseLessonPrerequisite,
    CourseRole,
)
from judge.utils.course_grades import (
    bulk_max_case_points_per_problem,
    bulk_calculate_lessons_progress,
)


def get_lesson_prerequisites_graph(course, valid_orders=None):
    """
    Build the prerequisites graph for a course.

    Args:
        course: Course object
        valid_orders: Optional set of valid lesson orders. If provided,
                      prerequisites referencing non-existent lessons are ignored.

    Returns:
        tuple: (adj_list, in_degree) where:
            - adj_list: {source_order: [(target_order, required_percentage), ...]}
            - in_degree: {target_order: number_of_prerequisites}
    """
    prerequisites = CourseLessonPrerequisite.objects.filter(course=course)

    adj_list = defaultdict(list)
    in_degree = defaultdict(int)

    for prereq in prerequisites:
        # Skip prerequisites where source or target lesson doesn't exist
        if valid_orders is not None:
            if prereq.source_order not in valid_orders:
                continue
            if prereq.target_order not in valid_orders:
                continue

        adj_list[prereq.source_order].append(
            (prereq.target_order, prereq.required_percentage)
        )
        in_degree[prereq.target_order] += 1

    return adj_list, in_degree


def get_lessons_by_order(course):
    """
    Get a mapping of lesson order to lesson object.

    Returns:
        dict: {order: lesson}
    """
    lessons = course.lessons.all()
    return {lesson.order: lesson for lesson in lessons}


def calculate_user_lesson_grades(user_profile, lessons):
    """
    Calculate the grade percentage for a user across all lessons.

    Args:
        user_profile: Profile object
        lessons: QuerySet or list of CourseLesson objects

    Returns:
        dict: {lesson_order: percentage}
    """

    if not lessons:
        return {}

    # Collect all problems from all lessons
    all_problems = []
    for lesson in lessons:
        all_problems.extend(lesson.get_problems())

    # Get problem points for this user
    bulk_problem_points = bulk_max_case_points_per_problem([user_profile], all_problems)

    # Calculate lesson progress
    lesson_progress = bulk_calculate_lessons_progress(
        [user_profile], lessons, bulk_problem_points
    )

    # Extract grades by lesson order
    user_grades = lesson_progress.get(user_profile, {})
    grades_by_order = {}

    for lesson in lessons:
        lesson_data = user_grades.get(lesson.id, {})
        grades_by_order[lesson.order] = lesson_data.get("percentage", 0)

    return grades_by_order


def update_lesson_unlock_states(user_profile, course):
    """
    BFS algorithm to propagate unlock states for a user in a course.

    Algorithm:
    1. Fetch lessons and prerequisites, build adjacency list
    2. Get current grades from existing progress records (fall back to calculated)
    3. Initialize: lessons with no prerequisites are unlocked
    4. BFS: For each unlocked lesson u, check if grade[u] >= w for edge (u,v,w)
    5. Decrement unmet_count[v], if 0 then unlock v
    6. Update CourseLessonProgress records in DB

    Args:
        user_profile: Profile object
        course: Course object

    Returns:
        list: List of newly unlocked lesson IDs
    """

    lessons = list(course.lessons.all())
    if not lessons:
        CourseRole.objects.filter(course=course, user=user_profile).update(
            needs_progress_recalculation=False
        )
        return []

    lessons_by_order = {lesson.order: lesson for lesson in lessons}
    lesson_orders = set(lessons_by_order.keys())

    # Get prerequisites graph (only include prerequisites for existing lessons)
    adj_list, in_degree = get_lesson_prerequisites_graph(
        course, valid_orders=lesson_orders
    )

    # Always calculate fresh grades for all lessons to ensure accuracy
    grades = calculate_user_lesson_grades(user_profile, lessons)

    # Track which lessons are unlocked
    unlocked_orders = set()
    unmet_count = dict(in_degree)

    # Initialize: lessons with no prerequisites are unlocked
    queue = deque()
    for order in lesson_orders:
        if in_degree.get(order, 0) == 0:
            unlocked_orders.add(order)
            queue.append(order)

    # BFS to propagate unlocks
    while queue:
        current_order = queue.popleft()
        current_grade = grades.get(current_order, 0)

        for target_order, required_percentage in adj_list.get(current_order, []):
            if target_order in unlocked_orders:
                continue

            # Check if this prerequisite is satisfied
            # Use small epsilon for float comparison
            if current_grade >= required_percentage - 0.01:
                unmet_count[target_order] = unmet_count.get(target_order, 0) - 1

                if unmet_count[target_order] <= 0:
                    unlocked_orders.add(target_order)
                    queue.append(target_order)

    # Update database - track newly unlocked lessons
    newly_unlocked = []

    existing = {
        row.lesson_id: row
        for row in CourseLessonProgress.objects.filter(
            user=user_profile, lesson__in=lessons
        )
    }
    created_rows, updated_rows = [], []
    for lesson in lessons:
        unlocked = lesson.order in unlocked_orders
        grade = grades.get(lesson.order, 0)
        row = existing.get(lesson.id)
        if row is None:
            created_rows.append(
                CourseLessonProgress(
                    user=user_profile,
                    lesson=lesson,
                    is_unlocked=unlocked,
                    percentage=grade,
                )
            )
            if unlocked:
                newly_unlocked.append(lesson.id)
        else:
            if unlocked and not row.is_unlocked:
                newly_unlocked.append(lesson.id)
            row.is_unlocked, row.percentage = unlocked, grade
            updated_rows.append(row)
    CourseLessonProgress.objects.bulk_create(created_rows, ignore_conflicts=True)
    CourseLessonProgress.objects.bulk_update(
        updated_rows, ["is_unlocked", "percentage"]
    )

    CourseRole.objects.filter(course=course, user=user_profile).update(
        needs_progress_recalculation=False
    )
    return newly_unlocked


def propagate_unlock_from_lesson(user_profile, lesson):
    """
    Trigger prerequisite recalculation when a lesson's grade changes.
    This is called from CourseLessonProgress.save() when percentage changes.

    Args:
        user_profile: Profile object
        lesson: CourseLesson object whose grade changed
    """
    course = lesson.course
    update_lesson_unlock_states(user_profile, course)


def initialize_user_course_progress(user_profile, course):
    """
    Initialize CourseLessonProgress records for a user when they join a course.
    This runs the initial unlock calculation.

    Args:
        user_profile: Profile object
        course: Course object

    Returns:
        list: List of unlocked lesson IDs
    """
    return update_lesson_unlock_states(user_profile, course)


def get_lesson_lock_status(user_profile, course):
    """
    Get the lock status for all lessons in a course for a user.

    Args:
        user_profile: Profile object
        course: Course object

    Returns:
        dict: {lesson_id: is_locked} - True if locked, False if unlocked
    """

    lessons = list(course.lessons.all())
    valid_orders = {lesson.order for lesson in lessons}

    needs_recalculation = CourseRole.objects.filter(
        course=course,
        user=user_profile,
        needs_progress_recalculation=True,
    ).exists()

    if needs_recalculation:
        update_lesson_unlock_states(user_profile, course)

    # Check if there are any valid prerequisites (both source and target exist)
    if not course.lesson_prerequisites.filter(
        source_order__in=valid_orders,
        target_order__in=valid_orders,
    ).exists():
        return {lesson.id: False for lesson in lessons}

    # Get existing progress records
    progress_records = CourseLessonProgress.objects.filter(
        user=user_profile, lesson__in=lessons
    ).values("lesson_id", "is_unlocked")

    progress_dict = {p["lesson_id"]: p["is_unlocked"] for p in progress_records}

    # Check if we need to initialize progress
    if len(progress_dict) < len(lessons):
        # Initialize missing progress records
        update_lesson_unlock_states(user_profile, course)
        # Refresh progress dict
        progress_records = CourseLessonProgress.objects.filter(
            user=user_profile, lesson__in=lessons
        ).values("lesson_id", "is_unlocked")
        progress_dict = {p["lesson_id"]: p["is_unlocked"] for p in progress_records}

    # Return lock status (inverted: is_locked = not is_unlocked)
    lock_status = {}
    for lesson in lessons:
        # Default to unlocked if no progress record (no prerequisites)
        is_unlocked = progress_dict.get(lesson.id, True)
        lock_status[lesson.id] = not is_unlocked

    return lock_status


def get_lesson_prerequisites_info(course):
    """
    Get prerequisite information for all lessons in a course.
    This is used to display what prerequisites are needed for each lesson.

    Only includes prerequisites where both source and target lessons exist.
    Orphan prerequisites (referencing non-existent lessons) are ignored.

    Args:
        course: Course object

    Returns:
        dict: {target_order: [(source_order, source_lesson_title, required_percentage), ...]}
    """
    prerequisites = CourseLessonPrerequisite.objects.filter(course=course)
    lessons_by_order = get_lessons_by_order(course)
    valid_orders = set(lessons_by_order.keys())

    prereq_info = defaultdict(list)
    for prereq in prerequisites:
        # Skip prerequisites where source or target lesson doesn't exist
        if prereq.source_order not in valid_orders:
            continue
        if prereq.target_order not in valid_orders:
            continue

        source_lesson = lessons_by_order.get(prereq.source_order)
        prereq_info[prereq.target_order].append(
            (prereq.source_order, source_lesson.title, prereq.required_percentage)
        )

    return prereq_info


def update_lesson_grade(user_profile, lesson):
    """
    Recalculate and update the grade percentage for a user's lesson.
    Called after submission/quiz attempt.

    Instead of immediately propagating unlock states (expensive), we mark
    the user's CourseRole.needs_progress_recalculation=True. The unlock
    algorithm will run lazily when the user visits the course page.

    Args:
        user_profile: Profile object
        lesson: CourseLesson object
    """

    course = lesson.course

    # Calculate current grade for just this lesson
    grades = calculate_user_lesson_grades(user_profile, [lesson])
    percentage = grades.get(lesson.order, 0)

    # Update or create progress record
    progress, created = CourseLessonProgress.objects.get_or_create(
        user=user_profile,
        lesson=lesson,
        defaults={"percentage": percentage, "is_unlocked": False},
    )

    grade_changed = False
    if created:
        grade_changed = True
    elif abs(progress.percentage - percentage) > 0.01:
        progress.percentage = percentage
        progress.save(update_fields=["percentage"])
        grade_changed = True

    # Mark user's course role for recalculation if grade changed
    if grade_changed:
        CourseRole.objects.filter(course=course, user=user_profile).update(
            needs_progress_recalculation=True
        )


def mark_course_for_recalculation(course):
    """
    Mark all enrolled users in a course as needing progress recalculation.
    Called when course structure changes (prerequisites, lessons, content).

    The actual recalculation happens lazily when each user visits the course page.

    Args:
        course: Course object
    """

    CourseRole.objects.filter(course=course).update(needs_progress_recalculation=True)


def require_lesson_access(lesson, user):
    """One gate for lesson pages, grades, and every lesson quiz operation."""
    profile = getattr(user, "profile", None) if user.is_authenticated else None
    if not Course.is_accessible_by(lesson.course, profile):
        raise PermissionDenied(_("You are not enrolled in this course."))
    if Course.is_editable_by(lesson.course, profile):
        return
    if not lesson.is_visible:
        raise Http404()
    if get_lesson_lock_status(profile, lesson.course).get(lesson.pk, True):
        raise PermissionDenied(
            _("This lesson is locked. Complete prerequisites first.")
        )


def reorder_course_items(course, model, raw_ids):
    ids = json.loads(raw_ids)
    if not isinstance(ids, list) or any(type(pk) is not int for pk in ids):
        raise ValidationError(_("Invalid item order."))
    with transaction.atomic():
        Course.objects.select_for_update().get(pk=course.pk)
        items = list(model.objects.select_for_update().filter(course=course))
        if len(ids) != len(items) or set(ids) != {item.pk for item in items}:
            raise ValidationError(_("Include every course item exactly once."))
        positions = {pk: order for order, pk in enumerate(ids, 1)}
        for item in items:
            item.order = positions[item.pk]
        model.objects.bulk_update(items, ["order"])
        # Prerequisites belong to positions, so their order pairs stay unchanged.
        mark_course_for_recalculation(course)
