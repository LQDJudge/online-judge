import fnmatch
import secrets

from django.core.files.storage import default_storage

from judge.utils.storage_helpers import storage_listdir, storage_delete_file


def delete_old_image_files(directory, base_filename):
    """
    Delete old image files with the given base filename pattern.
    This ensures that when a new image is uploaded, the old image is removed to save storage.
    Works with both local filesystem and S3 storage.

    Args:
        directory: The directory where images are stored (relative to storage root)
        base_filename: The base filename prefix (e.g., 'user_1')
                      Will match files like 'user_1_abc123.png' and 'user_1.png'
    """
    if not directory or not base_filename:
        return

    # Match both patterns:
    # 1. 'user_1.ext' (old files without suffix)
    # 2. 'user_1_*.ext' (new files with random suffix)
    patterns = [
        f"{base_filename}.*",
        f"{base_filename}_*.*",
    ]

    _, files = storage_listdir(default_storage, directory)
    for filename in files:
        for pattern in patterns:
            if fnmatch.fnmatch(filename, pattern):
                filepath = f"{directory}/{filename}" if directory else filename
                storage_delete_file(default_storage, filepath)
                break


def generate_image_filename(base_name, original_filename):
    """
    Generate a unique filename with a random suffix for CDN cache busting.

    Args:
        base_name: The base name for the file (e.g., 'user_1', 'organization_5')
        original_filename: The original uploaded filename to extract extension

    Returns:
        A filename like 'user_1_a1b2c3.png'
    """
    extension = original_filename.split(".")[-1].lower()
    random_suffix = secrets.token_hex(4)  # 8 character hex string
    return f"{base_name}_{random_suffix}.{extension}"
