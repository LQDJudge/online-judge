import os
import glob

from django.conf import settings


def delete_old_image_files(directory, base_filename):
    """
    Delete old image files with the given base filename (without extension).
    This ensures that when a new image is uploaded with a different extension,
    the old image is removed to save storage.

    Args:
        directory: The directory where images are stored (relative to MEDIA_ROOT)
        base_filename: The base filename without extension (e.g., 'user_1')
    """
    if not directory or not base_filename:
        return

    full_path = os.path.join(settings.MEDIA_ROOT, directory)
    pattern = os.path.join(full_path, f"{base_filename}.*")

    for file_path in glob.glob(pattern):
        try:
            os.remove(file_path)
        except OSError:
            pass
