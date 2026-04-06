"""
Views for AI-powered quiz question import from uploaded files.
Handles file upload, Celery task dispatch, and question/quiz creation.
"""

import json
import logging
import os
import re
import tempfile

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.http import JsonResponse
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic.base import TemplateResponseMixin

from judge.models import Quiz, QuizQuestion, QuizQuestionAssignment
from judge.models.quiz import QuizQuestionType
from judge.views.quiz import PendingGradingCountMixin
from judge.utils.views import TitleMixin

logger = logging.getLogger(__name__)

MAX_UPLOAD_SIZE = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".doc", ".docx"}


class QuizImportView(
    LoginRequiredMixin,
    PendingGradingCountMixin,
    TitleMixin,
    TemplateResponseMixin,
    View,
):
    """Main Import page — shows upload area and results."""

    template_name = "quiz/import.html"
    title = _("Import Questions")

    def get(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            from django.http import Http404

            raise Http404()

        cache_key = f"quiz_import_task_{request.user.id}"
        last_task_id = cache.get(cache_key)

        return self.render_to_response(
            {
                "page_type": "import",
                "max_upload_size": MAX_UPLOAD_SIZE,
                "last_task_id": last_task_id or "",
                "title": self.get_title(),
                "pending_grading_count": self.get_pending_grading_count(),
            }
        )


class QuizImportUploadView(View):
    """API endpoint: receives file upload, dispatches Celery analysis task."""

    def post(self, request):
        if not request.user.is_authenticated or not request.user.is_superuser:
            return JsonResponse({"error": _("Permission denied")}, status=403)

        upload = request.FILES.get("file")
        if not upload:
            return JsonResponse({"error": _("No file uploaded")}, status=400)

        # Validate extension
        ext = os.path.splitext(upload.name)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return JsonResponse(
                {
                    "error": _("Unsupported file type. Allowed: %(types)s")
                    % {"types": ", ".join(sorted(ALLOWED_EXTENSIONS))}
                },
                status=400,
            )

        if upload.size > MAX_UPLOAD_SIZE:
            size_mb = upload.size // (1024 * 1024)
            return JsonResponse(
                {
                    "error": _("File too large (%(size)s MB). Maximum is 50 MB.")
                    % {"size": size_mb}
                },
                status=400,
            )

        # Save to temp file
        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=ext, prefix="quiz_import__"
        )
        for chunk in upload.chunks():
            tmp.write(chunk)
        tmp.close()

        logger.info(
            "Quiz import upload: %s (%d bytes) -> %s",
            upload.name,
            upload.size,
            tmp.name,
        )

        # Dispatch Celery task
        try:
            from judge.tasks.quiz_import import quiz_import_task

            task = quiz_import_task.delay(tmp.name, request.user.id)
        except Exception:
            os.unlink(tmp.name)
            raise

        cache_key = f"quiz_import_task_{request.user.id}"
        cache.set(cache_key, task.id, 3600)

        return JsonResponse({"success": True, "task_id": task.id})


class QuizImportCreateQuestionView(View):
    """API endpoint: create a single QuizQuestion from import data."""

    def post(self, request):
        if not request.user.is_authenticated or not request.user.is_superuser:
            return JsonResponse({"error": _("Permission denied")}, status=403)

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": _("Invalid JSON")}, status=400)

        title = str(data.get("title", "")).strip()
        question_type = str(data.get("question_type", "")).upper()
        content = str(data.get("content", "")).strip()
        choices = data.get("choices")
        correct_answers = data.get("correct_answers")
        shuffle_choices = bool(data.get("shuffle_choices", False))
        is_public = bool(data.get("is_public", False))

        if not title:
            return JsonResponse({"error": _("Title is required")}, status=400)
        if not content:
            return JsonResponse({"error": _("Content is required")}, status=400)
        if question_type not in {c[0] for c in QuizQuestionType.choices}:
            return JsonResponse({"error": _("Invalid question type")}, status=400)

        # Truncate title if needed
        if len(title) > 255:
            title = title[:252] + "..."

        question = QuizQuestion.objects.create(
            title=title,
            question_type=question_type,
            content=content,
            choices=choices,
            correct_answers=correct_answers,
            shuffle_choices=shuffle_choices,
            is_public=is_public,
        )
        question.authors.add(request.profile)

        return JsonResponse(
            {
                "success": True,
                "question_id": question.pk,
                "question_url": reverse("question_bank_detail", args=[question.pk]),
            }
        )


class QuizImportCreateQuizView(View):
    """API endpoint: create a Quiz with specified questions."""

    def post(self, request):
        if not request.user.is_authenticated or not request.user.is_superuser:
            return JsonResponse({"error": _("Permission denied")}, status=403)

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": _("Invalid JSON")}, status=400)

        code = str(data.get("code", "")).strip()
        title = str(data.get("title", "")).strip()
        time_limit = data.get("time_limit", 0)
        shuffle_questions = bool(data.get("shuffle_questions", False))
        is_shown_answer = bool(data.get("is_shown_answer", False))
        is_public = bool(data.get("is_public", False))
        question_ids = data.get("question_ids", [])

        if not code:
            return JsonResponse({"error": _("Quiz code is required")}, status=400)
        if not title:
            return JsonResponse({"error": _("Quiz title is required")}, status=400)
        if not re.match(r"^[a-z0-9]+$", code):
            return JsonResponse(
                {
                    "error": _(
                        "Quiz code must contain only lowercase letters and digits"
                    )
                },
                status=400,
            )
        if Quiz.objects.filter(code=code).exists():
            return JsonResponse(
                {"error": _("Quiz code '%(code)s' already exists") % {"code": code}},
                status=400,
            )

        try:
            time_limit = int(time_limit)
            if time_limit < 0:
                time_limit = 0
        except (ValueError, TypeError):
            time_limit = 0

        # Verify all question IDs exist
        if not question_ids:
            return JsonResponse(
                {"error": _("At least one question is required")}, status=400
            )

        questions = QuizQuestion.objects.filter(pk__in=question_ids)
        if questions.count() != len(question_ids):
            return JsonResponse(
                {"error": _("Some questions were not found")}, status=400
            )

        quiz = Quiz.objects.create(
            code=code,
            title=title,
            time_limit=time_limit,
            shuffle_questions=shuffle_questions,
            is_shown_answer=is_shown_answer,
            is_public=is_public,
        )
        quiz.authors.add(request.profile)

        # Create question assignments in order
        for order, qid in enumerate(question_ids, start=1):
            QuizQuestionAssignment.objects.create(
                quiz=quiz,
                question_id=qid,
                order=order,
                points=1,
            )

        return JsonResponse(
            {
                "success": True,
                "quiz_url": reverse("quiz_edit", args=[quiz.code]),
            }
        )
