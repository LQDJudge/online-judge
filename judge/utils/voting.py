from judge.models.problem_review import ProblemReviewRun


def can_user_access_votable(user, obj):
    """Return whether a user may interact with votes for this object."""
    if obj is None:
        return False

    if isinstance(obj, ProblemReviewRun):
        return obj.problem.is_editable_by(user)

    access_check = getattr(obj, "is_accessible_by", None)
    if access_check is None:
        return False

    return access_check(user)
