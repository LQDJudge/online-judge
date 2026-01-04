import os

from django.conf import settings

from judge.caching import cache_wrapper


SAMPLE_BACKGROUNDS_DIR = os.path.join(settings.MEDIA_ROOT, "sample_backgrounds")


@cache_wrapper("sample_bgs", timeout=3600)
def get_sample_backgrounds():
    """
    Return list of sample background files from the folder.
    Results are cached for 1 hour.
    """
    backgrounds = []
    if os.path.exists(SAMPLE_BACKGROUNDS_DIR):
        for filename in sorted(os.listdir(SAMPLE_BACKGROUNDS_DIR)):
            if filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                name = os.path.splitext(filename)[0].replace("_", " ").replace("-", " ")
                backgrounds.append(
                    {
                        "name": name.title(),
                        "filename": filename,
                        "url": f"{settings.MEDIA_URL}sample_backgrounds/{filename}",
                    }
                )
    return backgrounds


def invalidate_sample_backgrounds_cache():
    """Invalidate the sample backgrounds cache."""
    get_sample_backgrounds.dirty()
