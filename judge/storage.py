from django.contrib.staticfiles.storage import ManifestStaticFilesStorage
import logging

logger = logging.getLogger("django.request")


class IgnoreMissingManifestStaticFilesStorage(ManifestStaticFilesStorage):
    def post_process(self, *args, **kwargs):
        try:
            for name, hashed_name, processed in super().post_process(*args, **kwargs):
                yield name, hashed_name, processed
        except ValueError as e:
            logger.warning(f"Ignoring static file error during post_process: {e}")

    def stored_name(self, name):
        try:
            return super().stored_name(name)
        except ValueError:
            logger.warning(f"Missing file in stored_name: {name}")
            return name

    def hashed_name(self, name, content=None, filename=None):
        try:
            return super().hashed_name(name, content, filename)
        except ValueError as e:
            logger.warning(f"Missing file in hashed_name: {name}, {e}")
            return name

    def url(self, name):
        try:
            return super().url(name)
        except ValueError as e:
            logger.warning(f"Missing file in url: {name}, {e}")
            # Fall back to the unhashed URL (skip hashing logic)
            return super(ManifestStaticFilesStorage, self).url(name)
