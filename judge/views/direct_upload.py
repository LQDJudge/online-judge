"""
API endpoints for direct file uploads (S3/R2 and local storage).
"""

import json

import reversion
from django.apps import apps
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.http import JsonResponse
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from judge.utils.upload_handler import UploadHandler
from judge.widgets.direct_upload import get_upload_token_data, UPLOAD_TOKEN_PREFIX

PAGEDOWN_IMAGE_MAX_SIZE = getattr(
    settings, "PAGEDOWN_IMAGE_UPLOAD_MAX_SIZE", 5 * 1024 * 1024
)
PAGEDOWN_IMAGE_UPLOAD_PATH = getattr(
    settings, "PAGEDOWN_IMAGE_UPLOAD_PATH", "pagedown-uploads"
)


@login_required
@require_POST
def get_upload_config(request):
    """
    Get presigned URL or local upload config.

    Request: upload_token, filename, content_type, file_size
    Response: upload_url, method, fields, file_key, file_url, token (local only)
    """
    try:
        if request.content_type == "application/json":
            data = json.loads(request.body)
        else:
            data = request.POST

        upload_token = data.get("upload_token", "").strip()
        filename = data.get("filename", "").strip()
        content_type = data.get("content_type", "application/octet-stream")
        file_size = int(data.get("file_size", 0))

    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": _("Invalid request data")}, status=400)

    if not upload_token:
        return JsonResponse({"error": _("Missing upload token")}, status=400)
    if not filename:
        return JsonResponse({"error": _("filename is required")}, status=400)

    token_data = get_upload_token_data(upload_token)
    if not token_data or token_data["profile_id"] != request.profile.id:
        return JsonResponse({"error": _("Invalid or expired token")}, status=400)

    try:
        config = UploadHandler.get_upload_config(
            profile=request.profile,
            upload_to=token_data["upload_to"],
            filename=filename,
            content_type=content_type,
            file_size=file_size,
            max_size=token_data["max_size"] or None,
            prefix=token_data["prefix"],
            object_id=token_data["object_id"],
        )
        return JsonResponse(config)

    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception:
        return JsonResponse(
            {"error": _("Failed to generate upload configuration")}, status=500
        )


@login_required
@csrf_exempt
@require_POST
def local_upload(request):
    """
    Handle file uploads for local storage (non-S3).

    Request: file (multipart), X-Upload-Token header
    Response: success, file_key, file_url
    """
    token = request.headers.get("X-Upload-Token", "")
    if not token:
        return JsonResponse({"error": _("Missing upload token")}, status=401)

    token_data = UploadHandler.verify_token(token, request.profile.id)
    if not token_data:
        return JsonResponse({"error": _("Invalid or expired token")}, status=401)

    uploaded_file = request.FILES.get("file")
    if not uploaded_file:
        return JsonResponse({"error": _("No file provided")}, status=400)

    max_size = token_data.get("max_size", 0)
    if max_size and uploaded_file.size > max_size:
        return JsonResponse(
            {"error": _("File size exceeds maximum allowed")}, status=400
        )

    try:
        saved_path = default_storage.save(token_data["file_key"], uploaded_file)
        return JsonResponse(
            {
                "success": True,
                "file_key": saved_path,
                "file_url": default_storage.url(saved_path),
            }
        )
    except Exception:
        return JsonResponse({"error": _("Failed to save file")}, status=500)


@login_required
@require_POST
def save_to_model(request):
    """
    Save uploaded file key to model field.

    Request: file_key, upload_token
    Response: success, message
    """
    try:
        if request.content_type == "application/json":
            data = json.loads(request.body)
        else:
            data = request.POST

        file_key = data.get("file_key", "").strip()
        upload_token = data.get("upload_token", "").strip()

    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": _("Invalid request data")}, status=400)

    if not file_key or not upload_token:
        return JsonResponse({"error": _("Missing required fields")}, status=400)

    cache_key = f"{UPLOAD_TOKEN_PREFIX}{upload_token}"
    token_data = cache.get(cache_key)

    if not token_data:
        return JsonResponse({"error": _("Invalid or expired token")}, status=403)

    if token_data["profile_id"] != request.profile.id:
        return JsonResponse({"error": _("Permission denied")}, status=403)

    try:
        app_label, model_class_name = token_data["model_name"].split(".")
        model_class = apps.get_model(app_label, model_class_name)
    except (ValueError, LookupError):
        return JsonResponse({"error": _("Invalid model")}, status=400)

    try:
        obj = model_class.objects.get(pk=token_data["object_id"])
    except model_class.DoesNotExist:
        return JsonResponse({"error": _("Object not found")}, status=404)

    # Verify actual file size on storage (defense against spoofed file_size)
    max_size = token_data.get("max_size")
    if max_size:
        try:
            actual_size = default_storage.size(file_key)
            if actual_size > max_size:
                try:
                    default_storage.delete(file_key)
                except Exception:
                    pass
                return JsonResponse(
                    {
                        "error": _(
                            "File size exceeds maximum allowed. File has been removed."
                        )
                    },
                    status=400,
                )
        except Exception:
            pass

    try:
        field_name = token_data["field_name"]
        old_file = getattr(obj, field_name, None)
        old_file_name = old_file.name if old_file else None

        if reversion.is_registered(type(obj)):
            with reversion.create_revision():
                setattr(obj, field_name, file_key)
                obj.save(update_fields=[field_name])
                reversion.set_user(request.user)
                reversion.set_comment(_("Updated %(field)s.") % {"field": field_name})
        else:
            setattr(obj, field_name, file_key)
            obj.save(update_fields=[field_name])

        if old_file_name and old_file_name != file_key:
            try:
                default_storage.delete(old_file_name)
            except Exception:
                pass

        return JsonResponse(
            {
                "success": True,
                "message": _("File saved successfully"),
            }
        )

    except Exception:
        return JsonResponse({"error": _("Failed to save file to model")}, status=500)


@login_required
@require_POST
def delete_file(request):
    """
    Delete file from model field.

    Request: upload_token
    Response: success, message
    """
    try:
        if request.content_type == "application/json":
            data = json.loads(request.body)
        else:
            data = request.POST

        upload_token = data.get("upload_token", "").strip()

    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": _("Invalid request data")}, status=400)

    if not upload_token:
        return JsonResponse({"error": _("Missing upload token")}, status=400)

    cache_key = f"{UPLOAD_TOKEN_PREFIX}{upload_token}"
    token_data = cache.get(cache_key)

    if not token_data:
        return JsonResponse({"error": _("Invalid or expired token")}, status=403)

    if token_data["profile_id"] != request.profile.id:
        return JsonResponse({"error": _("Permission denied")}, status=403)

    try:
        app_label, model_class_name = token_data["model_name"].split(".")
        model_class = apps.get_model(app_label, model_class_name)
    except (ValueError, LookupError):
        return JsonResponse({"error": _("Invalid model")}, status=400)

    try:
        obj = model_class.objects.get(pk=token_data["object_id"])
    except model_class.DoesNotExist:
        return JsonResponse({"error": _("Object not found")}, status=404)

    try:
        field_name = token_data["field_name"]
        old_file = getattr(obj, field_name, None)
        if old_file and old_file.name:
            try:
                default_storage.delete(old_file.name)
            except Exception:
                pass
            if reversion.is_registered(type(obj)):
                with reversion.create_revision():
                    setattr(obj, field_name, None)
                    obj.save(update_fields=[field_name])
                    reversion.set_user(request.user)
                    reversion.set_comment(
                        _("Removed %(field)s.") % {"field": field_name}
                    )
            else:
                setattr(obj, field_name, None)
                obj.save(update_fields=[field_name])

        return JsonResponse(
            {
                "success": True,
                "message": _("File deleted successfully"),
            }
        )

    except Exception:
        return JsonResponse({"error": _("Failed to delete file")}, status=500)


@login_required
@require_POST
def pagedown_upload_config(request):
    """
    Return a presigned upload config for pagedown image uploads.

    When the storage backend is S3/R2, the response contains a presigned PUT
    URL so the browser uploads image bytes directly to object storage,
    bypassing Django. When storage is local, the response signals a fallback
    to the legacy /pagedown/image-upload/ endpoint.

    Request: filename, content_type, file_size
    Response (S3): storage_type='s3', upload_url, method, file_key, file_url
    Response (local): storage_type='local'
    """
    if not getattr(settings, "PAGEDOWN_IMAGE_UPLOAD_ENABLED", False):
        return JsonResponse({"error": _("Image upload is disabled")}, status=400)

    try:
        if request.content_type == "application/json":
            data = json.loads(request.body)
        else:
            data = request.POST

        filename = data.get("filename", "").strip()
        content_type = data.get("content_type", "").strip()
        file_size = int(data.get("file_size", 0))
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": _("Invalid request data")}, status=400)

    if not filename:
        return JsonResponse({"error": _("filename is required")}, status=400)
    if not content_type.startswith("image/"):
        return JsonResponse({"error": _("Only image uploads are allowed")}, status=400)
    if file_size <= 0 or file_size > PAGEDOWN_IMAGE_MAX_SIZE:
        return JsonResponse(
            {"error": _("File size exceeds maximum allowed")}, status=400
        )

    try:
        config = UploadHandler.get_upload_config(
            profile=request.profile,
            upload_to=PAGEDOWN_IMAGE_UPLOAD_PATH,
            filename=filename,
            content_type=content_type,
            file_size=file_size,
            max_size=PAGEDOWN_IMAGE_MAX_SIZE,
            prefix="pd",
            object_id=request.profile.id,
        )
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception:
        return JsonResponse(
            {"error": _("Failed to generate upload configuration")}, status=500
        )

    if config.get("storage_type") != "s3":
        return JsonResponse({"storage_type": "local"})

    return JsonResponse(
        {
            "storage_type": "s3",
            "upload_url": config["upload_url"],
            "method": config["method"],
            "file_key": config["file_key"],
            "file_url": config["file_url"],
            "content_type": config["content_type"],
        }
    )
