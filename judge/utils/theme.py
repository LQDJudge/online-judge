from django.core.files.storage import default_storage

from judge.caching import cache_wrapper
from judge.utils.storage_helpers import storage_listdir

SAMPLE_BACKGROUNDS_PREFIX = "sample_backgrounds"


def is_sample_background_path(path):
    return bool(path) and (
        path == SAMPLE_BACKGROUNDS_PREFIX
        or path.startswith(f"{SAMPLE_BACKGROUNDS_PREFIX}/")
    )


@cache_wrapper("sample_bgs", timeout=3600)
def get_sample_backgrounds():
    """
    Return list of sample background files from storage.
    Results are cached for 1 hour.
    Works with both local filesystem and S3.
    """
    backgrounds = []
    _, filenames = storage_listdir(default_storage, SAMPLE_BACKGROUNDS_PREFIX)
    for filename in sorted(filenames):
        if filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            name = filename.rsplit(".", 1)[0].replace("_", " ").replace("-", " ")
            filepath = f"{SAMPLE_BACKGROUNDS_PREFIX}/{filename}"
            backgrounds.append(
                {
                    "name": name.title(),
                    "filename": filename,
                    "url": default_storage.url(filepath),
                }
            )
    return backgrounds


def invalidate_sample_backgrounds_cache():
    """Invalidate the sample backgrounds cache."""
    get_sample_backgrounds.dirty()
