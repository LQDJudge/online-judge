"""System-bot helpers for posting messages into the review comment thread.

The auto-review pipeline already produces machine-authored content (check
verdicts, AI synthesis). When an admin acts on a public request (approve /
reject), we also want that signal to land *inside* the review discussion
thread — so the author sees the admin's decision in the same place they've
been reading prior feedback. Notifications alone aren't enough: they get
dismissed quickly and don't show context next to the iteration history.

We post these as comments authored by a dedicated bot profile (username
"auto-review-bot") so the author can visually distinguish system messages
from human comments via the username/avatar.
"""

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext as _

from judge.models import Comment, Problem, Profile
from judge.models.problem_review import ProblemReviewRun
from judge.models.contest_review import ContestReviewRun
from judge.models.runtime import Language

# Username must match the `user_page` URL regex `\w+` (letters/digits/underscore
# only). Hyphens break `reverse("user_page", args=[username])`, which then
# crashes every comment-list template render that uses link_user(bot_profile).
SYSTEM_BOT_USERNAME = "auto_review_bot"


def get_or_create_system_bot_profile():
    """Return the singleton Profile used to author system comments.

    The underlying User is created lazily on first call, marked inactive
    with an unusable password so nobody can log in as it. The Profile is
    plain; no special permissions needed (it just owns Comment rows).
    """
    user, _ = User.objects.get_or_create(
        username=SYSTEM_BOT_USERNAME,
        defaults={
            "first_name": "Auto-review",
            "last_name": "Bot",
            "is_active": False,
        },
    )
    if user.has_usable_password():
        user.set_unusable_password()
        user.save(update_fields=["password"])

    # Profile.language is a non-null FK with a default callable
    # (Language.get_default_language_pk) that caches an int. The cached PK
    # may not exist in a freshly-created test DB. Always set language
    # explicitly to "first available" to dodge the FK violation.
    profile_defaults = {}
    any_language = Language.objects.first()
    if any_language is not None:
        profile_defaults["language"] = any_language

    profile, _ = Profile.objects.get_or_create(user=user, defaults=profile_defaults)
    return profile


def post_system_comment_on_review(problem, body):
    """Post a system-authored comment in `problem`'s review thread.

    Comments are anchored to the FIRST run for the problem (matching the
    dashboard view's anchor choice). If no run exists yet (e.g. legacy
    problem flow before auto-review), this is a no-op — there is no
    thread to post into.

    Returns the created Comment, or None if no anchor existed.
    """
    anchor = (
        ProblemReviewRun.objects.filter(problem=problem).order_by("started_at").first()
    )
    if anchor is None:
        return None

    bot_profile = get_or_create_system_bot_profile()
    ct = ContentType.objects.get_for_model(ProblemReviewRun)

    comment = Comment.objects.create(
        author=bot_profile,
        content_type=ct,
        object_id=anchor.id,
        body=body,
        score=0,
        hidden=False,
    )
    return comment


def post_system_comment_on_contest_review(contest, body):
    """Post a system-authored comment in `contest`'s review thread.

    Mirrors `post_system_comment_on_review` for problems. Anchored to the
    FIRST ContestReviewRun for the contest so admin decisions land alongside
    prior auto-review feedback (same UX rationale as problem reviews).
    """
    anchor = (
        ContestReviewRun.objects.filter(contest=contest).order_by("started_at").first()
    )
    if anchor is None:
        return None

    bot_profile = get_or_create_system_bot_profile()
    ct = ContentType.objects.get_for_model(ContestReviewRun)

    comment = Comment.objects.create(
        author=bot_profile,
        content_type=ct,
        object_id=anchor.id,
        body=body,
        score=0,
        hidden=False,
    )
    return comment


def post_publication_decision_comment(obj, status, reviewer, feedback=""):
    """Record the human publication decision, not a change to AI readiness."""
    reviewer_token = "[user:%s]" % reviewer.user.username
    if status == "A":
        body = _("**[System]** Publication request approved by %(name)s.") % {
            "name": reviewer_token
        }
    else:
        body = _("**[System]** Publication request rejected by %(name)s.") % {
            "name": reviewer_token
        }
    if feedback.strip():
        body += "\n\n" + "\n".join(
            "> " + line for line in feedback.strip().splitlines()
        )
    post = (
        post_system_comment_on_review
        if isinstance(obj, Problem)
        else post_system_comment_on_contest_review
    )
    return post(obj, body)


def post_contest_publication_decision_comment(contest, status, reviewer, feedback=""):
    return post_publication_decision_comment(contest, status, reviewer, feedback)
