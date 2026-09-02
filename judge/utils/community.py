from django.db.models import F


def has_solved_problem(profile):
    return profile.submission_set.filter(points=F("problem__points")).exists()


def can_use_community_features(user, profile):
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    return has_solved_problem(profile)
