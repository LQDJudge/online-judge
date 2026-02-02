"""
Storage-agnostic helper functions for file operations.

These helpers work with Django's storage abstraction (default_storage),
enabling seamless S3 migration via settings-only configuration.

Usage:
    from django.core.files.storage import default_storage
    from judge.utils.storage_helpers import storage_listdir, storage_file_exists

All functions accept a storage instance as the first parameter,
typically `default_storage`.
"""

import fnmatch
import mimetypes

from django.core.files.base import ContentFile
from django.http import HttpResponse, Http404
from django.utils import timezone


def storage_listdir(storage, path):
    """
    List files in a directory using storage API.

    Args:
        storage: Django storage instance (e.g., default_storage)
        path: Path to list (relative to storage root)

    Returns:
        Tuple of (directories, files) - both are lists of names
    """
    try:
        return storage.listdir(path)
    except FileNotFoundError:
        return ([], [])
    except OSError:
        return ([], [])


def storage_file_exists(storage, path):
    """
    Check if a file exists in storage.

    Args:
        storage: Django storage instance
        path: Path to check (relative to storage root)

    Returns:
        Boolean indicating if file exists
    """
    try:
        return storage.exists(path)
    except Exception:
        return False


def storage_delete_file(storage, path):
    """
    Delete a file from storage.

    Args:
        storage: Django storage instance
        path: Path to delete (relative to storage root)

    Returns:
        Boolean indicating success (True even if file didn't exist for S3)
    """
    try:
        storage.delete(path)
        return True
    except Exception:
        return False


def storage_get_file_size(storage, path):
    """
    Get file size from storage.

    Args:
        storage: Django storage instance
        path: Path to file (relative to storage root)

    Returns:
        File size in bytes, or 0 if file doesn't exist
    """
    try:
        return storage.size(path)
    except Exception:
        return 0


def storage_get_modified_time(storage, path):
    """
    Get file modification time from storage.

    Args:
        storage: Django storage instance
        path: Path to file (relative to storage root)

    Returns:
        datetime object, or None if unavailable
    """
    try:
        return storage.get_modified_time(path)
    except Exception:
        # Fallback for storage backends that don't support this
        return None


def storage_get_file_info(storage, path, filename):
    """
    Get file information (size, modified time, URL, mime type).

    Args:
        storage: Django storage instance
        path: Full path to file (relative to storage root)
        filename: Just the filename (for mime type detection)

    Returns:
        Dict with file info, or None if file doesn't exist
    """
    if not storage_file_exists(storage, path):
        return None

    modified = storage_get_modified_time(storage, path)
    if modified is None:
        modified = timezone.now()

    return {
        "name": filename,
        "size": storage_get_file_size(storage, path),
        "modified": modified,
        "url": storage.url(path),
        "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
    }


def storage_rename_file(storage, old_path, new_path):
    """
    Rename/move a file in storage.

    S3 doesn't support direct rename, so we copy + delete.
    Works with both local filesystem and S3.

    Args:
        storage: Django storage instance
        old_path: Current path (relative to storage root)
        new_path: New path (relative to storage root)

    Returns:
        Boolean indicating if rename was successful
    """
    try:
        with storage.open(old_path, "rb") as f:
            content = f.read()
        storage.save(new_path, ContentFile(content))
        storage.delete(old_path)
        return True
    except Exception:
        return False


def storage_save_file(storage, path, uploaded_file):
    """
    Save an uploaded file to storage.

    Args:
        storage: Django storage instance
        path: Path to save to (relative to storage root)
        uploaded_file: Django UploadedFile or file-like object

    Returns:
        The actual saved path (may differ if file exists)
    """
    return storage.save(path, uploaded_file)


def storage_open_file(storage, path, mode="rb"):
    """
    Open a file from storage.

    Args:
        storage: Django storage instance
        path: Path to file (relative to storage root)
        mode: File mode (default 'rb')

    Returns:
        File object
    """
    return storage.open(path, mode)


def storage_delete_matching_files(storage, directory, pattern):
    """
    Delete files matching a pattern in a directory.

    Args:
        storage: Django storage instance
        directory: Directory to search in
        pattern: Glob pattern to match (e.g., "user_1.*", "user_1_*.*")

    Returns:
        Number of files deleted
    """
    deleted_count = 0
    try:
        _, files = storage_listdir(storage, directory)
        for filename in files:
            if fnmatch.fnmatch(filename, pattern):
                filepath = f"{directory}/{filename}" if directory else filename
                if storage_delete_file(storage, filepath):
                    deleted_count += 1
    except Exception:
        pass
    return deleted_count


def storage_get_url(storage, path):
    """
    Get the URL for a file in storage.

    Args:
        storage: Django storage instance
        path: Path to file (relative to storage root)

    Returns:
        URL string for the file
    """
    return storage.url(path)


def validate_path_prefix(path, allowed_prefix):
    """
    Validate that a path starts with an allowed prefix.

    This replaces os.path.abspath() checks for S3 compatibility.

    Args:
        path: The path to validate
        allowed_prefix: The prefix the path must start with

    Returns:
        Boolean indicating if path is valid
    """
    # Normalize path to prevent directory traversal
    normalized = path.replace("\\", "/")

    # Remove any leading slashes for consistent comparison
    normalized = normalized.lstrip("/")
    prefix = allowed_prefix.lstrip("/")

    # Check for directory traversal attempts
    if ".." in normalized:
        return False

    return normalized.startswith(prefix)


def serve_file_with_nginx(
    request,
    storage,
    file_path,
    content_type="application/octet-stream",
    attachment_filename=None,
):
    """
    Serve a file with optimized delivery based on storage backend.

    - S3/boto storage: Redirect to signed URL
    - Local storage + Nginx: Use X-Accel-Redirect
    - Local storage (dev): Read file through Django

    Args:
        request: Django HttpRequest object
        storage: Django storage instance (e.g., default_storage)
        file_path: Path to file in storage (relative to storage root)
        content_type: MIME type for the response
        attachment_filename: If set, adds Content-Disposition header for download

    Returns:
        HttpResponse with file content, X-Accel-Redirect, or redirect

    Raises:
        Http404: If file doesn't exist
    """
    from django.conf import settings
    from django.http import HttpResponseRedirect

    # Check if using S3/remote storage (has custom URL generation)
    storage_url = storage.url(file_path)
    is_remote_storage = storage_url.startswith(("http://", "https://"))

    if is_remote_storage:
        # S3/boto: redirect to signed URL
        return HttpResponseRedirect(storage_url)

    # Local storage
    response = HttpResponse()

    # Check if we're behind Nginx
    use_nginx = request.META.get("SERVER_SOFTWARE", "").startswith("nginx/")

    if use_nginx:
        # Let Nginx serve the file directly using MEDIA_URL
        media_url = getattr(settings, "MEDIA_URL", "/media/")
        response["X-Accel-Redirect"] = f"{media_url}{file_path}"
    else:
        # Read file through Django (development)
        try:
            with storage.open(file_path, "rb") as f:
                response.content = f.read()
        except (IOError, FileNotFoundError):
            raise Http404()

    response["Content-Type"] = content_type

    if attachment_filename:
        response["Content-Disposition"] = (
            f'attachment; filename="{attachment_filename}"'
        )

    return response


def serve_file_inline(
    request,
    storage,
    file_path,
    content_type="application/octet-stream",
    inline_filename=None,
):
    """
    Serve a file inline (for viewing in browser) with optimized delivery.

    - S3/boto storage: Redirect to signed URL
    - Local storage + Nginx: Use X-Accel-Redirect
    - Local storage (dev): Read file through Django

    Args:
        request: Django HttpRequest object
        storage: Django storage instance
        file_path: Path to file in storage
        content_type: MIME type for the response
        inline_filename: Filename for Content-Disposition header

    Returns:
        HttpResponse with file content, X-Accel-Redirect, or redirect

    Raises:
        Http404: If file doesn't exist
    """
    from django.conf import settings
    from django.http import HttpResponseRedirect

    # Check if using S3/remote storage
    storage_url = storage.url(file_path)
    is_remote_storage = storage_url.startswith(("http://", "https://"))

    if is_remote_storage:
        # S3/boto: redirect to signed URL
        return HttpResponseRedirect(storage_url)

    # Local storage
    response = HttpResponse()

    # Check if we're behind Nginx
    use_nginx = request.META.get("SERVER_SOFTWARE", "").startswith("nginx/")

    if use_nginx:
        media_url = getattr(settings, "MEDIA_URL", "/media/")
        response["X-Accel-Redirect"] = f"{media_url}{file_path}"
    else:
        try:
            with storage.open(file_path, "rb") as f:
                response.content = f.read()
        except (IOError, FileNotFoundError):
            raise Http404()

    response["Content-Type"] = content_type

    if inline_filename:
        response["Content-Disposition"] = f"inline; filename={inline_filename}"

    return response
