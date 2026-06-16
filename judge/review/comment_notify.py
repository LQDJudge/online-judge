"""Notify the right people when someone comments on a review thread.

The generic comment-notification path (`judge/views/comment/actions.py`) only
notifies `target_object.authors`. Review threads are anchored to a
`ProblemReviewRun` / `ContestReviewRun`, which have no `.authors`, so a
review-thread comment would otherwise notify nobody.

This helper fills that gap. When anyone comments on a review thread it notifies:
  - all superusers (the review-coordinator audience), and
  - the reviewed item's authors + curators (so setters hear admin replies even
    without an @mention),
excluding the comment's own author (handled by the notification manager) and
the parent-comment author (who already gets a REPLY notification).

It is a no-op for any target that isn't a review run, so the caller can invoke
it unconditionally from the generic comment flow.
"""

import logging

from judge.models.contest_review import ContestReviewRun
from judge.models.notification import Notification, NotificationCategory
from judge.models.problem_review import ProblemReviewRun
from judge.models.profile import Profile

logger = logging.getLogger(__name__)


def _reviewed_item(target_object):
    """Return the Problem/Contest behind a review run, or None if not a run."""
    if isinstance(target_object, ProblemReviewRun):
        return target_object.problem
    if isinstance(target_object, ContestReviewRun):
        return target_object.contest
    return None


def notify_review_comment(comment, target_object, html_link):
    """Notify superusers + item authors/curators about a review-thread comment.

    No-op when `target_object` isn't a review run.
    """
    item = _reviewed_item(target_object)
    if item is None:
        return

    # Item setters: authors + curators (the people responsible for the item).
    recipient_ids = set(item.authors.values_list("id", flat=True))
    recipient_ids |= set(item.curators.values_list("id", flat=True))

    # Review-coordinator audience = all superusers.
    recipient_ids |= set(
        Profile.objects.filter(user__is_superuser=True).values_list("id", flat=True)
    )

    # The parent-comment author already receives a REPLY notification; don't
    # double-notify them here. (The comment's own author is excluded by
    # bulk_create_notifications via the `author` argument.)
    if comment.parent_id and comment.parent.author_id:
        recipient_ids.discard(comment.parent.author_id)
    recipient_ids.discard(comment.author_id)

    if not recipient_ids:
        return

    Notification.objects.bulk_create_notifications(
        user_ids=list(recipient_ids),
        category=NotificationCategory.REVIEW_COMMENT,
        html_link=html_link,
        author=comment.author,
    )
