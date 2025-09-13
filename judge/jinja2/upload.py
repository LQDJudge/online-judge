from judge.jinja2 import registry
from judge.views.custom_file_upload import check_upload_permission


@registry.filter
def can_upload_files(user):
    """Check if a user has permission to use the upload feature."""
    if not user or not user.is_authenticated:
        return False

    return check_upload_permission(user)
