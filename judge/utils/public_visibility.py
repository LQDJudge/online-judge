"""Site-wide publication scope, shared by admission and explicit Lock backfill.

A visibility flag alone is insufficient: organization-, user- and course-restricted
content is not treated as published to the entire site.
"""

from django.db.models import Q

PROBLEM_PUBLIC_SCOPE = {"is_public": True, "is_organization_private": False}
CONTEST_PUBLIC_SCOPE = {
    "is_visible": True,
    "is_private": False,
    "is_organization_private": False,
    "is_in_course": False,
}


def site_public_problem_q():
    return Q(**PROBLEM_PUBLIC_SCOPE)


def site_public_contest_q():
    return Q(**CONTEST_PUBLIC_SCOPE)


def is_site_public(obj):
    scope = (
        PROBLEM_PUBLIC_SCOPE
        if obj._meta.model_name == "problem"
        else CONTEST_PUBLIC_SCOPE
    )
    return all(getattr(obj, name) == expected for name, expected in scope.items())
