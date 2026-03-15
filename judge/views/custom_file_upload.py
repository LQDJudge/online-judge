import json
import mimetypes
import os
from datetime import datetime

from django.conf import settings
from django.shortcuts import render, redirect
from django.core.files.storage import default_storage
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import Http404, JsonResponse, HttpResponseForbidden
from django.urls import reverse
from django.utils.translation import gettext as _
from django.template.defaultfilters import filesizeformat
from django.views.decorators.http import require_POST

from judge.models import Problem
from judge.caching import cache_wrapper

from judge.utils.upload_handler import UploadHandler
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
from judge.widgets.direct_upload import generate_upload_token

MEDIA_PATH = "user_uploads"


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


def _is_s3_storage():
    return hasattr(default_storage, "bucket")


def _get_download_url(file_path, filename):
    """Generate a download URL with Content-Disposition: attachment."""
    if _is_s3_storage():
        client = default_storage.connection.meta.client
        bucket_name = default_storage.bucket_name
        full_key = file_path
        if hasattr(default_storage, "location") and default_storage.location:
            full_key = f"{default_storage.location}/{file_path}"
        return client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket_name,
                "Key": full_key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=3600,
        )
    return reverse("user_file_download", args=[filename])


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

    # Add download URLs (presigned for S3, direct for local)
    user_prefix = get_user_storage_path(username)
    for f in files:
        f["download_url"] = _get_download_url(f"{user_prefix}/{f['name']}", f["name"])

    # Generate upload token for direct upload
    upload_token = generate_upload_token(
        profile_id=request.profile.id,
        model_name="",
        object_id=None,
        field_name="",
        max_size=max_file_size,
        upload_to=get_user_storage_path(username),
        prefix="upload",
    )

    context = {
        "storage": storage_info,
        "files": files,
        "title": _("My Files"),
        "max_file_size": max_file_size,
        "max_files": settings.DMOJ_MAX_FILES_PER_USER,
        "is_admin": request.user.is_superuser,
        "upload_token": upload_token,
        "upload_config_url": reverse("user_file_upload_config"),
        "upload_confirm_url": reverse("user_file_upload_confirm"),
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
    """Download a user's file with Content-Disposition: attachment (local storage only)."""
    if not check_upload_permission(request.user):
        return HttpResponseForbidden(
            _("You don't have permission to use the upload feature.")
        )

    username = request.user.username
    user_prefix = get_user_storage_path(username)
    file_path = f"{user_prefix}/{filename}"

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
                    new_download_url = _get_download_url(new_file_path, new_filename)

                    # Invalidate cache
                    get_user_files.dirty(username)

                    if is_ajax:
                        return JsonResponse(
                            {
                                "success": True,
                                "message": _("File renamed successfully!"),
                                "new_name": new_filename,
                                "new_url": new_url,
                                "new_download_url": new_download_url,
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


@login_required
@require_POST
def user_file_upload_config(request):
    """
    Get presigned URL or local upload config for user file upload.
    Validates permissions, file count limit, and storage quota.
    """
    if not check_upload_permission(request.user):
        return JsonResponse({"error": _("Permission denied")}, status=403)

    try:
        if request.content_type == "application/json":
            data = json.loads(request.body)
        else:
            data = request.POST

        filename = data.get("filename", "").strip()
        content_type = data.get("content_type", "application/octet-stream")
        file_size = int(data.get("file_size", 0))
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": _("Invalid request data")}, status=400)

    if not filename:
        return JsonResponse({"error": _("Filename is required")}, status=400)
    if file_size <= 0:
        return JsonResponse({"error": _("Invalid file size")}, status=400)

    username = request.user.username
    max_file_size, max_user_storage = get_user_limits(request.user)

    # Check file size limit
    if file_size > max_file_size:
        return JsonResponse(
            {"error": _("File size cannot exceed %s" % filesizeformat(max_file_size))},
            status=400,
        )

    # Check file count limit
    files = get_user_files(username)
    if len(files) >= settings.DMOJ_MAX_FILES_PER_USER:
        return JsonResponse(
            {
                "error": _(
                    "Maximum number of files reached (%d files). Please delete some files."
                    % settings.DMOJ_MAX_FILES_PER_USER
                )
            },
            status=400,
        )

    # Check storage quota
    total_used = sum(f["size"] for f in files)
    if total_used + file_size > max_user_storage:
        return JsonResponse(
            {
                "error": _(
                    "Storage quota exceeded (%s limit). Please delete some files."
                    % filesizeformat(max_user_storage)
                )
            },
            status=400,
        )

    try:
        config = UploadHandler.get_upload_config(
            profile=request.profile,
            upload_to=get_user_storage_path(username),
            filename=filename,
            content_type=content_type,
            file_size=file_size,
            max_size=max_file_size,
        )
        return JsonResponse(config)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception:
        return JsonResponse(
            {"error": _("Failed to generate upload configuration")}, status=500
        )


@login_required
@require_POST
def user_file_upload_confirm(request):
    """
    Confirm a direct upload completed. Invalidates cache and returns updated storage info.
    """
    if not check_upload_permission(request.user):
        return JsonResponse({"error": _("Permission denied")}, status=403)

    try:
        if request.content_type == "application/json":
            data = json.loads(request.body)
        else:
            data = request.POST

        file_key = data.get("file_key", "").strip()
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": _("Invalid request data")}, status=400)

    if not file_key:
        return JsonResponse({"error": _("Missing file_key")}, status=400)

    username = request.user.username
    user_prefix = get_user_storage_path(username)

    # Path traversal check: file_key must be within user's storage path
    if not validate_path_prefix(file_key, user_prefix):
        return JsonResponse({"error": _("Invalid file path")}, status=400)

    # Verify actual file size on storage (defense against spoofed file_size)
    max_file_size, max_user_storage = get_user_limits(request.user)
    try:
        actual_size = storage_get_file_size(default_storage, file_key)
    except Exception:
        return JsonResponse({"error": _("File not found on storage")}, status=400)

    if actual_size > max_file_size:
        # Delete the oversized file
        try:
            storage_delete_file(default_storage, file_key)
        except Exception:
            pass
        return JsonResponse(
            {"error": _("File size exceeds maximum allowed. File has been removed.")},
            status=400,
        )

    # Check total storage quota with actual size
    get_user_files.dirty(username)
    files = get_user_files(username)
    total_used = sum(f["size"] for f in files)
    if total_used > max_user_storage:
        try:
            storage_delete_file(default_storage, file_key)
        except Exception:
            pass
        get_user_files.dirty(username)
        return JsonResponse(
            {"error": _("Storage quota exceeded. File has been removed.")},
            status=400,
        )

    # Get updated storage info
    updated_storage = get_user_storage_usage(username, request.user)
    file_url = default_storage.url(file_key)
    file_name = os.path.basename(file_key)

    return JsonResponse(
        {
            "success": True,
            "file_url": file_url,
            "file_name": file_name,
            "storage": {
                "used": updated_storage["used"],
                "max": updated_storage["max"],
                "percentage": updated_storage["percentage"],
                "used_formatted": filesizeformat(updated_storage["used"]),
                "max_formatted": filesizeformat(updated_storage["max"]),
            },
        }
    )
