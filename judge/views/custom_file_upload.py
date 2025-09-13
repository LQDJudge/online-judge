import os
import mimetypes
from datetime import datetime
from urllib.parse import urljoin

from django.shortcuts import render, redirect
from django.core.files.storage import FileSystemStorage
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse, Http404, JsonResponse, HttpResponseForbidden
from django.conf import settings
from django.utils.translation import gettext as _
from django import forms
from django.template.defaultfilters import filesizeformat
from judge.models import Problem

MEDIA_PATH = "user_uploads"

# Storage limits for different user types
ADMIN_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB per file for admins
ADMIN_MAX_USER_STORAGE = 100 * 1024 * 1024  # 100MB total for admins
NORMAL_MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB per file for normal users
NORMAL_MAX_USER_STORAGE = 30 * 1024 * 1024  # 30MB total for normal users
MAX_FILES_PER_USER = 100  # Maximum 100 files per user


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
        return ADMIN_MAX_FILE_SIZE, ADMIN_MAX_USER_STORAGE
    else:
        return NORMAL_MAX_FILE_SIZE, NORMAL_MAX_USER_STORAGE


def get_user_folder(username):
    """Get the user's upload folder path"""
    return os.path.join(settings.MEDIA_ROOT, MEDIA_PATH, username)


def get_user_files(username):
    """Get all files for a user from filesystem"""
    user_folder = get_user_folder(username)
    files = []

    if os.path.exists(user_folder):
        for filename in os.listdir(user_folder):
            file_path = os.path.join(user_folder, filename)
            if os.path.isfile(file_path):
                stat = os.stat(file_path)
                files.append(
                    {
                        "name": filename,
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime),
                        "url": urljoin(
                            settings.MEDIA_URL, f"{MEDIA_PATH}/{username}/{filename}"
                        ),
                        "mime_type": mimetypes.guess_type(filename)[0]
                        or "application/octet-stream",
                    }
                )

    # Sort by modified date, newest first
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
    storage_info = get_user_storage_usage(username, request.user)

    if request.method == "POST":
        form = FileUploadForm(request.POST, request.FILES)
        if form.is_valid():
            file = request.FILES["file"]
            is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

            # Check number of files limit
            current_files = get_user_files(username)
            if len(current_files) >= MAX_FILES_PER_USER:
                error_msg = _(
                    f"Maximum number of files reached ({MAX_FILES_PER_USER} files). Please delete some files."
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

            # Create user-specific folder
            user_folder = f"{MEDIA_PATH}/{username}"

            # Sanitize filename
            file_name, file_extension = os.path.splitext(file.name)
            file_name = "".join(
                c for c in file_name if c.isalnum() or c in (" ", "-", "_")
            ).rstrip()
            if not file_name:
                file_name = "file"

            # Use original filename, add counter only if duplicate exists
            new_filename = f"{file_name}{file_extension}"
            fs = FileSystemStorage(
                location=os.path.join(settings.MEDIA_ROOT, user_folder)
            )

            # Check if file exists and add counter if needed
            if fs.exists(new_filename):
                counter = 1
                while fs.exists(f"{file_name}_{counter}{file_extension}"):
                    counter += 1
                new_filename = f"{file_name}_{counter}{file_extension}"

            # Save file
            saved_filename = fs.save(new_filename, file)

            if is_ajax:
                # Get updated storage info
                updated_storage = get_user_storage_usage(username, request.user)
                return JsonResponse(
                    {
                        "success": True,
                        "message": _("File uploaded successfully!"),
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

    # Get user files for display
    files = get_user_files(username)

    context = {
        "form": form,
        "storage": storage_info,
        "files": files,
        "title": _("My Files"),
        "max_file_size": max_file_size,
        "max_files": MAX_FILES_PER_USER,
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
    user_folder = get_user_folder(username)
    file_path = os.path.join(user_folder, filename)

    # Security check - ensure file is in user's folder
    if not os.path.abspath(file_path).startswith(os.path.abspath(user_folder)):
        raise Http404("File not found")

    if request.method == "POST":
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        if os.path.exists(file_path) and os.path.isfile(file_path):
            os.remove(file_path)

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
    user_folder = get_user_folder(username)
    file_path = os.path.join(user_folder, filename)

    # Security check - ensure file is in user's folder
    if not os.path.abspath(file_path).startswith(os.path.abspath(user_folder)):
        raise Http404("File not found")

    if os.path.exists(file_path) and os.path.isfile(file_path):
        with open(file_path, "rb") as f:
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            response = HttpResponse(f.read(), content_type=mime_type)
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response
    else:
        raise Http404("File not found")


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
    user_folder = get_user_folder(username)
    old_file_path = os.path.join(user_folder, filename)

    # Security check - ensure file is in user's folder
    if not os.path.abspath(old_file_path).startswith(os.path.abspath(user_folder)):
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
        new_file_path = os.path.join(user_folder, new_filename)

        # Check if new filename already exists
        if os.path.exists(new_file_path) and new_filename != filename:
            error_msg = _("A file with this name already exists")
            if is_ajax:
                return JsonResponse({"success": False, "error": error_msg}, status=400)
            messages.error(request, error_msg)
            return redirect("custom_file_upload")

        # Rename the file
        if os.path.exists(old_file_path) and os.path.isfile(old_file_path):
            try:
                os.rename(old_file_path, new_file_path)

                if is_ajax:
                    return JsonResponse(
                        {
                            "success": True,
                            "message": _("File renamed successfully!"),
                            "new_name": new_filename,
                            "new_url": urljoin(
                                settings.MEDIA_URL,
                                f"{MEDIA_PATH}/{username}/{new_filename}",
                            ),
                        }
                    )

                messages.success(request, _("File renamed successfully!"))
            except Exception as e:
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
