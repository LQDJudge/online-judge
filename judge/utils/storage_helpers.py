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
        Boolean indicating if deletion was successful
    """
    try:
        if storage.exists(path):
            storage.delete(path)
            return True
        return False
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
        if not storage.exists(old_path):
            return False

        # Read the file content
        with storage.open(old_path, "rb") as f:
            content = f.read()

        # Save to new location
        storage.save(new_path, ContentFile(content))

        # Delete old file
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
