"""Human review decisions on contest public requests.

Single source of truth for the side effects of an admin Accept/Reject on a
ContestPublicRequest: set status/feedback/reviewed_by, post a system comment
into the contest review thread, and notify the requesting author.

IMPORTANT: a decision is *only* a review verdict. It deliberately does NOT
touch Contest.is_visible / is_rated — publishing a contest (and making it
rated) is a separate manual admin step done later, after the admin finalizes
timing with the author. Both the review-page endpoints and the Django-admin
ContestPublicRequest action delegate here so "accept"/"reject" mean exactly
the same thing everywhere.
"""

import logging

from django.urls import reverse
from django.utils.translation import gettext as _

from judge.models.contest_review import ContestPublicRequest
from judge.models.notification import Notification, NotificationCategory
from judge.review.system_bot import post_system_comment_on_contest_review

logger = logging.getLogger(__name__)


def accept_contest_public_request(contest, admin_profile, feedback=""):
    """Record an Accept verdict. Returns the ContestPublicRequest, or None."""
    return _decide(contest, ContestPublicRequest.APPROVED, admin_profile, feedback)


def reject_contest_public_request(contest, admin_profile, feedback=""):
    """Record a Reject verdict. Returns the ContestPublicRequest, or None."""
    return _decide(contest, ContestPublicRequest.REJECTED, admin_profile, feedback)


def _decide(contest, status, admin_profile, feedback):
    pr = ContestPublicRequest.objects.filter(contest=contest).first()
    if pr is None:
        # Nothing to decide on — admin opened a contest that never had a
        # public request (e.g. an admin-rerun-only review).
        return None

    status_changed = pr.status != status
    pr.status = status
    pr.feedback = feedback or ""
    pr.reviewed_by = admin_profile
    pr.save(update_fields=["status", "feedback", "reviewed_by", "updated_at"])

    # Only announce on a real status transition. Re-clicking the same
    # verdict (or just editing feedback) must not spam a duplicate comment
    # or re-notify the author.
    if status_changed:
        post_contest_decision_side_effects(
            contest, status, admin_profile, pr.feedback, pr=pr
        )
    return pr


def post_contest_decision_side_effects(
    contest, status, admin_profile, feedback, pr=None
):
    """Outward effects of a decision: system comment + author notification.

    Shared by the review-page endpoints (via _decide, which also writes the
    DB row) and the Django-admin action (which persists the row itself via
    the admin form and calls this only for the comment + notification). The
    caller is responsible for deciding WHEN to emit (i.e. only on a real
    status transition).
    """
    if pr is None:
        pr = ContestPublicRequest.objects.filter(contest=contest).first()
    if pr is None:
        return
    _post_comment(contest, status, admin_profile, feedback)
    _notify(contest, status, admin_profile, pr)


def _post_comment(contest, status, admin_profile, feedback):
    # `[user:NAME]` renders as a rank-colored mention via the comment parser —
    # same convention used elsewhere in the review threads.
    reviewer_token = "[user:%s]" % admin_profile.user.username
    if status == ContestPublicRequest.APPROVED:
        body = _("**[System]** Review accepted by %(name)s.") % {"name": reviewer_token}
    else:
        body = _("**[System]** Review rejected by %(name)s.") % {"name": reviewer_token}
    if feedback:
        body += "\n\n> " + feedback
    try:
        post_system_comment_on_contest_review(contest, body)
    except Exception:
        # The comment is nice-to-have; never let it sink the decision.
        logger.exception("Failed to post decision comment for %s", contest.key)


def _notify(contest, status, admin_profile, pr):
    if not pr.requested_by:
        return
    if status == ContestPublicRequest.APPROVED:
        category = NotificationCategory.CONTEST_PUBLIC_REQUEST_APPROVED
        try:
            url = reverse("contest_view", args=[contest.key])
        except Exception:
            url = "/contest/%s/" % contest.key
    else:
        category = NotificationCategory.CONTEST_PUBLIC_REQUEST_REJECTED
        try:
            url = reverse("contest_review_dashboard", args=[contest.key])
        except Exception:
            url = "/contest/%s/review/" % contest.key
    html_link = '<a href="%(url)s">%(name)s</a>' % {"url": url, "name": contest.name}
    Notification.objects.create_notification(
        owner=pr.requested_by,
        category=category,
        html_link=html_link,
        author=admin_profile,
    )
