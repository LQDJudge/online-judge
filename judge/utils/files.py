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


def generate_secure_filename(original_filename, prefix=None):
    """
    Generate a secure filename with a random suffix to prevent URL guessing.

    Args:
        original_filename: The original uploaded filename
        prefix: Optional prefix (e.g., 'user_1', 'problem_code')

    Returns:
        A filename like 'myfile_a1b2c3d4.png' or 'user_1_myfile_a1b2c3d4.png'
    """
    if "." in original_filename:
        base_name, extension = original_filename.rsplit(".", 1)
        extension = "." + extension.lower()
    else:
        base_name = original_filename
        extension = ""

    base_name = "".join(
        c for c in base_name if c.isalnum() or c in (" ", "-", "_")
    ).rstrip()
    if not base_name:
        base_name = "file"

    random_suffix = secrets.token_hex(4)

    if prefix:
        return f"{prefix}_{random_suffix}{extension}"
    return f"{base_name}_{random_suffix}{extension}"
