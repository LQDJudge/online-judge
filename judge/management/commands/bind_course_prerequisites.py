import json
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from judge.models import Course, CourseLesson, CourseLessonPrerequisite
from judge.utils.course_prerequisites import mark_course_for_recalculation


class Command(BaseCommand):
    help = "Audit legacy lesson-order prerequisites; optionally bind only unambiguous lesson identities."

    def add_arguments(self, parser):
        parser.add_argument("--course", type=int)
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        courses = Course.objects.all()
        if options["course"]:
            courses = courses.filter(pk=options["course"])
        if options["apply"]:
            courses = courses.select_for_update()
        course_ids = list(courses.values_list("pk", flat=True))
        by_order = defaultdict(list)
        lesson_courses = {}
        for pk, course_id, order in CourseLesson.objects.filter(
            course_id__in=course_ids
        ).values_list("pk", "course_id", "order"):
            by_order[course_id, order].append(pk)
            lesson_courses[pk] = course_id
        edges = list(CourseLessonPrerequisite.objects.filter(course_id__in=course_ids))
        ready, unresolved = [], []
        for edge in edges:
            for side in ("source", "target"):
                pk = getattr(edge, side + "_lesson_id")
                matches = (
                    [pk]
                    if pk
                    else by_order[edge.course_id, getattr(edge, side + "_order")]
                )
                if (
                    len(matches) != 1
                    or lesson_courses.get(matches[0]) != edge.course_id
                ):
                    unresolved.append(
                        {
                            "edge": edge.pk,
                            "course": edge.course_id,
                            "side": side,
                            "order": getattr(edge, side + "_order"),
                            "matches": matches,
                        }
                    )
                else:
                    setattr(edge, side + "_lesson_id", matches[0])
            if edge.source_lesson_id and edge.target_lesson_id:
                ready.append(edge)
        self.stdout.write(
            json.dumps(
                {
                    "courses": len(course_ids),
                    "edges": len(edges),
                    "resolvable": len(ready),
                    "unresolved": unresolved,
                    "apply": options["apply"],
                },
                indent=2,
            )
        )
        if options["apply"]:
            if unresolved:
                raise CommandError(
                    "Unresolved prerequisite identities; no changes applied. Repair the reported mappings first."
                )
            CourseLessonPrerequisite.objects.bulk_update(
                ready, ["source_lesson", "target_lesson"]
            )
            from_ids = {edge.course_id for edge in ready}
            for course in courses:
                if course.pk in from_ids:
                    mark_course_for_recalculation(course)
