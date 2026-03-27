"""
Views for AI-powered problem package import.
Handles zip upload, Celery task dispatch, status polling, and per-field Apply.
"""

import logging
import os
import re
import tempfile
import zipfile

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin

from judge.models import Problem, ProblemData, Language
from judge.views.problem import ProblemMixin, TitleMixin

logger = logging.getLogger(__name__)

# Max upload size: 50MB (Poe CDN limit)
MAX_UPLOAD_SIZE = 50 * 1024 * 1024

# Allowed image extensions for upload
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}


def _validate_path_in_dir(base_dir, filename):
    """
    Validate that a filename resolves to a path inside base_dir.
    Prevents path traversal attacks via ../ or symlinks.
    Returns the validated real path, or raises ValueError.
    """
    real_base = os.path.realpath(base_dir)
    joined = os.path.join(base_dir, filename)
    real_path = os.path.realpath(joined)
    if not real_path.startswith(real_base + os.sep) and real_path != real_base:
        raise ValueError("Invalid file path")
    return real_path


def _validate_save_dir(save_dir):
    """Validate save_dir is a real temp directory (resolves symlinks)."""
    if not save_dir:
        raise ValueError("Missing save_dir")
    real_dir = os.path.realpath(save_dir)
    if not real_dir.startswith("/tmp/") or not os.path.isdir(real_dir):
        raise ValueError("Invalid save_dir")
    return real_dir


class PackageImportView(
    ProblemMixin,
    TitleMixin,
    TemplateResponseMixin,
    SingleObjectMixin,
    View,
):
    """Main Import tab page — shows upload area or import results."""

    template_name = "problem/import.html"

    def get_title(self):
        return _("Import Package - {0}").format(self.object.name)

    def has_permission(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser

    def get(self, request, *args, **kwargs):
        if not self.has_permission():
            raise Http404()

        self.object = self.get_object()

        # Check for a previous import task result in cache
        from django.core.cache import cache

        cache_key = f"import_task_{self.object.code}_{request.user.id}"
        last_task_id = cache.get(cache_key)

        return self.render_to_response(
            {
                "problem": self.object,
                "title": self.get_title(),
                "content_title": mark_safe(
                    _("Import Package for %s")
                    % (
                        '<a href="%s">%s</a>'
                        % (
                            self.object.get_absolute_url(),
                            escape(self.object.name),
                        )
                    )
                ),
                "max_upload_size": MAX_UPLOAD_SIZE,
                "last_task_id": last_task_id or "",
            }
        )


class PackageImportUploadView(View):
    """API endpoint: receives zip upload, dispatches Celery analysis task."""

    def post(self, request, problem):
        if not request.user.is_authenticated or not request.user.is_superuser:
            return JsonResponse({"error": "Permission denied"}, status=403)

        problem_obj = get_object_or_404(Problem, code=problem)

        # Get uploaded file
        upload = request.FILES.get("package_file")
        if not upload:
            return JsonResponse({"error": "No file uploaded"}, status=400)

        if not upload.name.endswith(".zip"):
            return JsonResponse({"error": "File must be a .zip"}, status=400)

        if upload.size > MAX_UPLOAD_SIZE:
            size_mb = upload.size // (1024 * 1024)
            return JsonResponse(
                {"error": f"File too large ({size_mb} MB). Maximum is 50 MB."},
                status=400,
            )

        # Validate it's a valid zip (fix #5: use context manager)
        try:
            with zipfile.ZipFile(upload):
                pass
            upload.seek(0)
        except zipfile.BadZipFile:
            return JsonResponse({"error": "Invalid zip file"}, status=400)

        # Save to temp file
        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=".zip", prefix=f"import_{problem}__"
        )
        for chunk in upload.chunks():
            tmp.write(chunk)
        tmp.close()

        logger.info(
            "Package upload for %s: %s (%d bytes) -> %s",
            problem,
            upload.name,
            upload.size,
            tmp.name,
        )

        # Dispatch Celery task (fix #6: clean up temp file on dispatch failure)
        try:
            from judge.tasks.package_import import package_import_task

            task = package_import_task.delay(problem_obj.code, tmp.name)
        except Exception:
            os.unlink(tmp.name)
            raise

        # Cache the task ID so the Import page can recover after refresh
        from django.core.cache import cache

        cache_key = f"import_task_{problem}_{request.user.id}"
        cache.set(cache_key, task.id, 3600)  # 1 hour TTL

        return JsonResponse({"success": True, "task_id": task.id})


class PackageImportFileView(View):
    """API endpoint: read a file from the import temp dir for preview."""

    def get(self, request, problem):
        if not request.user.is_authenticated or not request.user.is_superuser:
            return JsonResponse({"error": "Permission denied"}, status=403)

        file_path = request.GET.get("path", "")
        if not file_path or not file_path.startswith("/tmp/"):
            return JsonResponse({"error": "Invalid path"}, status=400)

        # Security: resolve symlinks and verify still in /tmp/
        real_path = os.path.realpath(file_path)
        if not real_path.startswith("/tmp/"):
            return JsonResponse({"error": "Invalid path"}, status=400)

        if not os.path.exists(real_path):
            return JsonResponse({"error": "File not found"}, status=404)

        try:
            with open(real_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(100000)  # Max 100KB preview
            return HttpResponse(content, content_type="text/plain; charset=utf-8")
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)


class PackageImportApplyView(View):
    """API endpoint: applies a single imported field to the problem."""

    def post(self, request, problem):
        if not request.user.is_authenticated or not request.user.is_superuser:
            return JsonResponse({"error": "Permission denied"}, status=403)

        problem_obj = get_object_or_404(Problem, code=problem)

        field = request.POST.get("field")
        save_dir = request.POST.get("save_dir")

        # Fix #3: validate save_dir with realpath to prevent symlink bypass
        try:
            save_dir = _validate_save_dir(save_dir)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)

        try:
            result = self._apply_field(
                problem_obj, field, save_dir, request.POST, request.user
            )
            return JsonResponse({"success": True, "message": result})
        except Exception as e:
            logger.error("Apply error for %s/%s: %s", problem, field, e, exc_info=True)
            return JsonResponse({"error": str(e)}, status=500)

    def _apply_field(self, problem, field, save_dir, post_data, user=None):
        """Apply a single field from the import result to the problem."""

        if field == "description":
            return self._apply_description(problem, save_dir, user)
        elif field == "time_limit":
            return self._apply_time_limit(problem, post_data)
        elif field == "memory_limit":
            return self._apply_memory_limit(problem, post_data)
        elif field == "testdata":
            return self._apply_testdata(problem, save_dir)
        elif field == "checker":
            return self._apply_checker(problem, save_dir)
        elif field == "generator":
            return self._apply_generator(problem, save_dir)
        elif field == "generator_script":
            return self._apply_generator_script(problem, save_dir)
        elif field == "interactive":
            return self._apply_interactive(problem, save_dir)
        elif field.startswith("solution_"):
            return self._apply_solution(problem, save_dir, field, post_data)
        else:
            raise ValueError(f"Unknown field: {field}")

    def _apply_description(self, problem, save_dir, user=None):
        path = _validate_path_in_dir(save_dir, "description.md")
        if not os.path.exists(path):
            raise FileNotFoundError("description.md not found")
        with open(path, "r", encoding="utf-8") as f:
            description = f.read()

        # Upload images and update references in description
        image_count = 0

        def replace_image_ref(match):
            nonlocal image_count
            alt_text = match.group(1)
            img_ref = match.group(2)

            # Skip if already a full URL
            if img_ref.startswith(("http://", "https://", "/")):
                return match.group(0)

            # Fix #2: sanitize image filename
            img_name = os.path.basename(img_ref)
            # Validate extension
            ext = os.path.splitext(img_name)[1].lower()
            if ext not in ALLOWED_IMAGE_EXTENSIONS:
                return match.group(0)

            # Validate path stays inside save_dir
            try:
                img_path = _validate_path_in_dir(save_dir, img_name)
            except ValueError:
                return match.group(0)

            if not os.path.exists(img_path):
                return match.group(0)

            # Upload to user's upload directory
            username = user.username if user else "import"
            # Sanitize filename: keep only alphanumeric, dash, underscore, dot
            safe_name = re.sub(r"[^\w.\-]", "_", img_name)
            upload_dir = f"user_uploads/{username}"
            dest_path = f"{upload_dir}/{safe_name}"

            with open(img_path, "rb") as img_f:
                saved_path = default_storage.save(dest_path, ContentFile(img_f.read()))

            img_url = f"/media/{saved_path}"
            image_count += 1
            return f"![{alt_text}]({img_url})"

        description = re.sub(
            r"!\[([^\]]*)\]\(([^)]+)\)", replace_image_ref, description
        )

        problem.description = description
        problem.save(update_fields=["description"])

        msg = "Description updated"
        if image_count:
            msg += f" ({image_count} image{'s' if image_count > 1 else ''} uploaded)"
        return msg

    def _apply_time_limit(self, problem, post_data):
        # Fix #7: validate time limit bounds
        try:
            value = float(post_data.get("value", 1.0))
        except (ValueError, TypeError):
            raise ValueError("Invalid time limit value")
        min_tl = getattr(settings, "DMOJ_PROBLEM_MIN_TIME_LIMIT", 0)
        max_tl = getattr(settings, "DMOJ_PROBLEM_MAX_TIME_LIMIT", 60)
        value = max(min_tl, min(value, max_tl))
        problem.time_limit = value
        problem.save(update_fields=["time_limit"])
        return f"Time limit set to {value}s"

    def _apply_memory_limit(self, problem, post_data):
        # Fix #7: validate memory limit bounds
        try:
            value_mb = int(post_data.get("value", 256))
        except (ValueError, TypeError):
            raise ValueError("Invalid memory limit value")
        max_kb = getattr(settings, "DMOJ_PROBLEM_MAX_MEMORY_LIMIT", 1048576)
        min_kb = getattr(settings, "DMOJ_PROBLEM_MIN_MEMORY_LIMIT", 0)
        value_kb = max(min_kb, min(value_mb * 1024, max_kb))
        problem.memory_limit = value_kb
        problem.save(update_fields=["memory_limit"])
        actual_mb = value_kb // 1024
        msg = f"Memory limit set to {actual_mb} MB"
        if value_mb * 1024 > max_kb:
            msg += f" (capped from {value_mb} MB)"
        return msg

    def _apply_testdata(self, problem, save_dir):
        path = _validate_path_in_dir(save_dir, "testdata.zip")
        if not os.path.exists(path):
            raise FileNotFoundError("testdata.zip not found")
        data, _ = ProblemData.objects.get_or_create(problem=problem)
        with open(path, "rb") as f:
            data.zipfile.save("testdata.zip", ContentFile(f.read()))
        data.save()
        return "Test data zip uploaded"

    def _apply_checker(self, problem, save_dir):
        path = _validate_path_in_dir(save_dir, "checker.cpp")
        if not os.path.exists(path):
            raise FileNotFoundError("checker.cpp not found")
        data, _ = ProblemData.objects.get_or_create(problem=problem)
        with open(path, "rb") as f:
            data.custom_checker_cpp.save("checker.cpp", ContentFile(f.read()))
        data.checker = "customcpp"
        data.save()
        return "Checker uploaded (type: customcpp)"

    def _apply_generator(self, problem, save_dir):
        path = _validate_path_in_dir(save_dir, "generator.cpp")
        if not os.path.exists(path):
            raise FileNotFoundError("generator.cpp not found")
        data, _ = ProblemData.objects.get_or_create(problem=problem)
        with open(path, "rb") as f:
            data.generator.save("generator.cpp", ContentFile(f.read()))
        data.save()
        return "Generator uploaded"

    def _apply_generator_script(self, problem, save_dir):
        path = _validate_path_in_dir(save_dir, "generator_script.txt")
        if not os.path.exists(path):
            raise FileNotFoundError("generator_script.txt not found")
        data, _ = ProblemData.objects.get_or_create(problem=problem)
        with open(path, "r", encoding="utf-8") as f:
            data.generator_script = f.read()
        data.save(update_fields=["generator_script"])
        return "Generator script updated"

    def _apply_interactive(self, problem, save_dir):
        path = _validate_path_in_dir(save_dir, "interactive.cpp")
        if not os.path.exists(path):
            raise FileNotFoundError("interactive.cpp not found")
        data, _ = ProblemData.objects.get_or_create(problem=problem)
        with open(path, "rb") as f:
            data.interactive_judge.save("interactive.cpp", ContentFile(f.read()))
        data.checker = "interact"
        data.save()
        return "Interactive judge uploaded (type: interact)"

    def _apply_solution(self, problem, save_dir, field, post_data):
        from judge.models import ProblemSolutionCode

        filename = post_data.get("filename", "")
        if not filename:
            raise ValueError("Missing solution filename")

        # Fix #1: validate path stays inside save_dir
        path = _validate_path_in_dir(save_dir, filename)
        if not os.path.exists(path):
            raise FileNotFoundError(f"{filename} not found")

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source_code = f.read()

        # Determine language from extension
        ext = os.path.splitext(filename)[1].lower()
        lang_map = {
            ".cpp": "CPP17",
            ".cc": "CPP17",
            ".c": "C",
            ".java": "JAVA17",
            ".py": "PY3",
            ".rb": "RUBY",
            ".rs": "RUST",
            ".go": "GO",
            ".kt": "KOTLIN",
        }
        lang_key = lang_map.get(ext, "CPP17")
        language = Language.objects.filter(key=lang_key).first()

        # Determine expected result from filename (sol_ac_name.cpp → AC)
        expected_result = "AC"
        name_lower = filename.lower()
        if "_wa_" in name_lower or name_lower.startswith("sol_wa"):
            expected_result = "WA"
        elif "_tle_" in name_lower or name_lower.startswith("sol_tle"):
            expected_result = "TLE"
        elif "_mle_" in name_lower or name_lower.startswith("sol_mle"):
            expected_result = "MLE"
        elif "_rte_" in name_lower or name_lower.startswith("sol_rte"):
            expected_result = "RTE"

        # Get next order
        last_order = (
            ProblemSolutionCode.objects.filter(problem=problem)
            .order_by("-order")
            .values_list("order", flat=True)
            .first()
        ) or 0

        ProblemSolutionCode.objects.create(
            problem=problem,
            order=last_order + 1,
            name=filename,
            source_code=source_code,
            language=language,
            expected_result=expected_result,
        )
        return f"Solution '{filename}' added ({expected_result})"
