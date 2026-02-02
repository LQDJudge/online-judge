import mimetypes
import os
from datetime import datetime

from django.conf import settings
from django.shortcuts import render, redirect
from django.core.files.storage import default_storage
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import Http404, JsonResponse, HttpResponseForbidden
from django.utils.translation import gettext as _
from django import forms
from django.template.defaultfilters import filesizeformat

from judge.models import Problem
from judge.caching import cache_wrapper

from judge.utils.files import generate_secure_filename
from judge.utils.storage_helpers import (
    serve_file_with_nginx,
    storage_listdir,
    storage_file_exists,
    storage_delete_file,
    storage_get_file_size,
    storage_get_modified_time,
    storage_rename_file,
    validate_path_prefix,
)

MEDIA_PATH = "user_uploads"


class FileUploadForm(forms.Form):
    file = forms.FileField()


def check_upload_permission(user):
    """Check if user has permission to use the upload feature"""
    if not user.is_authenticated:
        return False

    # Superusers always have access
    if user.is_superuser:
        return True

    # Check if user has the edit_own_problem or edit_all_problem permission
    if user.has_perm("judge.edit_own_problem") or user.has_perm(
        "judge.edit_all_problem"
    ):
        return True

    # Check if user is an author or curator of any problem
    profile = user.profile
    if (
        Problem.objects.filter(authors=profile).exists()
        or Problem.objects.filter(curators=profile).exists()
    ):
        return True

    return False


def get_user_limits(user):
    """Get storage limits based on user type"""
    if user.is_superuser:
        return settings.DMOJ_ADMIN_MAX_FILE_SIZE, settings.DMOJ_ADMIN_MAX_STORAGE
    else:
        return settings.DMOJ_USER_MAX_FILE_SIZE, settings.DMOJ_USER_MAX_STORAGE


def get_user_storage_path(username):
    """Get the user's upload folder path (relative to storage root)"""
    return f"{MEDIA_PATH}/{username}"


@cache_wrapper(prefix="guf")
def get_user_files(username):
    """Get all files for a user from storage"""
    user_prefix = get_user_storage_path(username)
    files = []

    if hasattr(default_storage, "bucket"):
        prefix = f"{user_prefix}/"
        if hasattr(default_storage, "location") and default_storage.location:
            prefix = f"{default_storage.location}/{prefix}"
        for obj in default_storage.bucket.objects.filter(Prefix=prefix):
            filename = obj.key.split("/")[-1]
            if not filename:
                continue
            filepath = f"{user_prefix}/{filename}"
            files.append(
                {
                    "name": filename,
                    "size": obj.size,
                    "modified": obj.last_modified,
                    "url": default_storage.url(filepath),
                    "mime_type": mimetypes.guess_type(filename)[0]
                    or "application/octet-stream",
                }
            )
    else:
        _, filenames = storage_listdir(default_storage, user_prefix)
        for filename in filenames:
            filepath = f"{user_prefix}/{filename}"
            size = storage_get_file_size(default_storage, filepath)
            modified = storage_get_modified_time(default_storage, filepath)
            files.append(
                {
                    "name": filename,
                    "size": size,
                    "modified": modified if modified else datetime.now(),
                    "url": default_storage.url(filepath),
                    "mime_type": mimetypes.guess_type(filename)[0]
                    or "application/octet-stream",
                }
            )

    files.sort(key=lambda x: x["modified"], reverse=True)
    return files


def get_user_storage_usage(username, user):
    """Calculate total storage used by user"""
    files = get_user_files(username)
    total_size = sum(f["size"] for f in files)
    _, max_storage = get_user_limits(user)
    return {
        "used": total_size,
        "max": max_storage,
        "percentage": int((total_size / max_storage) * 100) if max_storage > 0 else 0,
    }


@login_required
def file_upload(request):
    """Handle file uploads for all users - single page with upload and file list"""
    # Check if user has permission to use upload feature
    if not check_upload_permission(request.user):
        return HttpResponseForbidden(
            _(
                "You don't have permission to use the upload feature. Only users who can edit problems are allowed."
            )
        )

    username = request.user.username
    max_file_size, max_user_storage = get_user_limits(request.user)
    files = get_user_files(username)
    storage_info = get_user_storage_usage(username, request.user)

    if request.method == "POST":
        form = FileUploadForm(request.POST, request.FILES)
        if form.is_valid():
            file = request.FILES["file"]
            is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

            if len(files) >= settings.DMOJ_MAX_FILES_PER_USER:
                error_msg = _(
                    f"Maximum number of files reached ({settings.DMOJ_MAX_FILES_PER_USER} files). Please delete some files."
                )
                if is_ajax:
                    return JsonResponse(
                        {"success": False, "error": error_msg}, status=400
                    )
                messages.error(request, error_msg)
                return redirect("custom_file_upload")

            # Check file size
            if file.size > max_file_size:
                error_msg = _(
                    f"File size cannot exceed {filesizeformat(max_file_size)}"
                )
                if is_ajax:
                    return JsonResponse(
                        {"success": False, "error": error_msg}, status=400
                    )
                messages.error(request, error_msg)
                return redirect("custom_file_upload")

            # Check storage quota
            if storage_info["used"] + file.size > max_user_storage:
                error_msg = _(
                    f"Storage quota exceeded ({filesizeformat(max_user_storage)} limit). Please delete some files."
                )
                if is_ajax:
                    return JsonResponse(
                        {"success": False, "error": error_msg}, status=400
                    )
                messages.error(request, error_msg)
                return redirect("custom_file_upload")

            # User storage path prefix
            user_prefix = get_user_storage_path(username)

            # Generate secure filename with random suffix
            new_filename = generate_secure_filename(file.name)
            new_filepath = f"{user_prefix}/{new_filename}"

            # Save file using default_storage
            saved_path = default_storage.save(new_filepath, file)
            file_url = default_storage.url(saved_path)

            # Invalidate cache
            get_user_files.dirty(username)

            if is_ajax:
                # Get updated storage info
                updated_storage = get_user_storage_usage(username, request.user)
                return JsonResponse(
                    {
                        "success": True,
                        "message": _("File uploaded successfully!"),
                        "file_url": file_url,
                        "file_name": new_filename,
                        "storage": {
                            "used": updated_storage["used"],
                            "max": updated_storage["max"],
                            "percentage": updated_storage["percentage"],
                            "used_formatted": filesizeformat(updated_storage["used"]),
                            "max_formatted": filesizeformat(updated_storage["max"]),
                        },
                    }
                )

            messages.success(request, _("File uploaded successfully!"))
            return redirect("custom_file_upload")
    else:
        form = FileUploadForm()

    context = {
        "form": form,
        "storage": storage_info,
        "files": files,
        "title": _("My Files"),
        "max_file_size": max_file_size,
        "max_files": settings.DMOJ_MAX_FILES_PER_USER,
        "is_admin": request.user.is_superuser,
    }
    return render(request, "user_upload/upload.html", context)


@login_required
def user_file_list(request):
    """List all files for the current user - redirect to main upload page"""
    # Check permission
    if not check_upload_permission(request.user):
        return HttpResponseForbidden(
            _("You don't have permission to use the upload feature.")
        )
    return redirect("custom_file_upload")


@login_required
def user_file_delete(request, filename):
    """Delete a user's file"""
    # Check permission
    if not check_upload_permission(request.user):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {"success": False, "error": "Permission denied"}, status=403
            )
        return HttpResponseForbidden(
            _("You don't have permission to use the upload feature.")
        )

    username = request.user.username
    user_prefix = get_user_storage_path(username)
    file_path = f"{user_prefix}/{filename}"

    # Security check - ensure file is in user's folder (prevents path traversal)
    if not validate_path_prefix(file_path, user_prefix):
        raise Http404("File not found")

    if request.method == "POST":
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        if storage_file_exists(default_storage, file_path):
            storage_delete_file(default_storage, file_path)

            # Invalidate cache
            get_user_files.dirty(username)

            if is_ajax:
                # Get updated storage info
                updated_storage = get_user_storage_usage(username, request.user)
                return JsonResponse(
                    {
                        "success": True,
                        "message": _("File deleted successfully!"),
                        "storage": {
                            "used": updated_storage["used"],
                            "max": updated_storage["max"],
                            "percentage": updated_storage["percentage"],
                            "used_formatted": filesizeformat(updated_storage["used"]),
                            "max_formatted": filesizeformat(updated_storage["max"]),
                        },
                    }
                )

            messages.success(request, _("File deleted successfully!"))
        else:
            if is_ajax:
                return JsonResponse(
                    {"success": False, "error": _("File not found")}, status=404
                )
            messages.error(request, _("File not found"))

        return redirect("custom_file_upload")

    # For GET request, just redirect to file list
    return redirect("custom_file_upload")


@login_required
def user_file_download(request, filename):
    """Download a user's file"""
    # Check permission
    if not check_upload_permission(request.user):
        return HttpResponseForbidden(
            _("You don't have permission to use the upload feature.")
        )

    username = request.user.username
    user_prefix = get_user_storage_path(username)
    file_path = f"{user_prefix}/{filename}"

    # Security check - ensure file is in user's folder (prevents path traversal)
    if not validate_path_prefix(file_path, user_prefix):
        raise Http404("File not found")

    if not storage_file_exists(default_storage, file_path):
        raise Http404("File not found")

    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return serve_file_with_nginx(
        request,
        default_storage,
        file_path,
        content_type=mime_type,
        attachment_filename=filename,
    )


@login_required
def user_file_rename(request, filename):
    """Rename a user's file"""
    # Check permission
    if not check_upload_permission(request.user):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {"success": False, "error": "Permission denied"}, status=403
            )
        return HttpResponseForbidden(
            _("You don't have permission to use the upload feature.")
        )

    username = request.user.username
    user_prefix = get_user_storage_path(username)
    old_file_path = f"{user_prefix}/{filename}"

    # Security check - ensure file is in user's folder (prevents path traversal)
    if not validate_path_prefix(old_file_path, user_prefix):
        raise Http404("File not found")

    if request.method == "POST":
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        new_name = request.POST.get("new_name", "").strip()

        if not new_name:
            error_msg = _("New filename cannot be empty")
            if is_ajax:
                return JsonResponse({"success": False, "error": error_msg}, status=400)
            messages.error(request, error_msg)
            return redirect("custom_file_upload")

        # Preserve the file extension
        old_name, old_ext = os.path.splitext(filename)
        new_name_base, new_ext = os.path.splitext(new_name)

        # If no extension provided in new name, use the old extension
        if not new_ext:
            new_ext = old_ext

        # Sanitize the new filename
        new_name_base = "".join(
            c for c in new_name_base if c.isalnum() or c in (" ", "-", "_")
        ).rstrip()
        if not new_name_base:
            new_name_base = "file"

        new_filename = f"{new_name_base}{new_ext}"
        new_file_path = f"{user_prefix}/{new_filename}"

        # Check if new filename already exists
        if (
            storage_file_exists(default_storage, new_file_path)
            and new_filename != filename
        ):
            error_msg = _("A file with this name already exists")
            if is_ajax:
                return JsonResponse({"success": False, "error": error_msg}, status=400)
            messages.error(request, error_msg)
            return redirect("custom_file_upload")

        # Rename the file using copy+delete pattern (S3 compatible)
        if storage_file_exists(default_storage, old_file_path):
            try:
                if storage_rename_file(default_storage, old_file_path, new_file_path):
                    new_url = default_storage.url(new_file_path)

                    # Invalidate cache
                    get_user_files.dirty(username)

                    if is_ajax:
                        return JsonResponse(
                            {
                                "success": True,
                                "message": _("File renamed successfully!"),
                                "new_name": new_filename,
                                "new_url": new_url,
                            }
                        )

                    messages.success(request, _("File renamed successfully!"))
                else:
                    raise Exception("Rename failed")
            except Exception:
                error_msg = _("Failed to rename file")
                if is_ajax:
                    return JsonResponse(
                        {"success": False, "error": error_msg}, status=500
                    )
                messages.error(request, error_msg)
        else:
            if is_ajax:
                return JsonResponse(
                    {"success": False, "error": _("File not found")}, status=404
                )
            messages.error(request, _("File not found"))

        return redirect("custom_file_upload")

    # For GET request, just redirect to file list
    return redirect("custom_file_upload")
