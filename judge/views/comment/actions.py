import json

from django.conf import settings
from django.contrib.auth.context_processors import PermWrapper
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.db.models import F
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotFound,
)
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from reversion import revisions

from judge.models import (
    Comment,
    CommentVote,
    Problem,
    Contest,
    BlogPost,
    OrganizationModerationLog,
)
from judge.models.problem import Solution
from judge.models.notification import Notification, NotificationCategory
from judge.models.comment import (
    get_visible_comment_count,
    get_visible_top_level_comment_count,
)
from judge.utils.ratelimit import ratelimit
from judge.views.comment.forms import CommentForm
from judge.views.comment.mixins import is_comment_locked
from judge.views.comment.utils import (
    get_html_link_notification,
    add_mention_notifications,
)


@ratelimit(key="user", rate=settings.RL_VOTE)
@login_required
def vote_comment(request, delta):
    if abs(delta) != 1:
        return HttpResponseBadRequest(
            _("Messing around, are we?"), content_type="text/plain"
        )

    if request.method != "POST":
        return HttpResponseForbidden()

    if "id" not in request.POST:
        return HttpResponseBadRequest()

    if (
        not request.user.is_staff
        and not request.profile.submission_set.filter(
            points=F("problem__points")
        ).exists()
    ):
        return HttpResponseBadRequest(
            _("You must solve at least one problem before you can vote."),
            content_type="text/plain",
        )

    try:
        comment_id = int(request.POST["id"])
    except ValueError:
        return HttpResponseBadRequest()
    else:
        if not Comment.objects.filter(id=comment_id).exists():
            raise Http404()

    vote = CommentVote()
    vote.comment_id = comment_id
    vote.voter = request.profile
    vote.score = delta

    try:
        vote.save()
    except IntegrityError:
        try:
            vote = CommentVote.objects.get(comment_id=comment_id, voter=request.profile)
        except CommentVote.DoesNotExist:
            raise Http404()
        if -vote.score != delta:
            return HttpResponseBadRequest(
                _("You already voted."), content_type="text/plain"
            )
        vote.delete()
        Comment.objects.filter(id=comment_id).update(score=F("score") - vote.score)
    else:
        Comment.objects.filter(id=comment_id).update(score=F("score") + delta)

    # Dirty comment cache since we updated score via QuerySet.update()
    Comment.dirty_cache(comment_id)
    return HttpResponse("success", content_type="text/plain")


def upvote_comment(request):
    return vote_comment(request, 1)


def downvote_comment(request):
    return vote_comment(request, -1)


def _can_hide_comment(request, comment):
    """
    Check if user can hide this comment.
    Returns True if user has permission or is author of the content.
    """
    # Global permission to hide any comment
    if request.user.has_perm("judge.change_comment"):
        return True

    if not request.user.is_authenticated:
        return False

    profile = request.profile

    # Check if user is author of the content the comment is on
    content_type = comment.content_type
    object_id = comment.object_id

    # BlogPost authors
    if content_type.model == "blogpost":
        try:
            blog = BlogPost.objects.get(id=object_id)
            if blog.authors.filter(id=profile.id).exists():
                return True
        except BlogPost.DoesNotExist:
            pass

    # Problem authors
    elif content_type.model == "problem":
        try:
            problem = Problem.objects.get(id=object_id)
            if problem.authors.filter(id=profile.id).exists():
                return True
        except Problem.DoesNotExist:
            pass

    # Contest authors
    elif content_type.model == "contest":
        try:
            contest = Contest.objects.get(id=object_id)
            if contest.authors.filter(id=profile.id).exists():
                return True
        except Contest.DoesNotExist:
            pass

    # Solution authors
    elif content_type.model == "solution":
        try:
            solution = Solution.objects.get(id=object_id)
            if solution.authors.filter(id=profile.id).exists():
                return True
        except Solution.DoesNotExist:
            pass

    return False


@require_POST
def comment_hide(request):
    try:
        comment_id = int(request.POST["id"])
    except ValueError:
        return HttpResponseBadRequest()

    comment = get_object_or_404(Comment, id=comment_id)

    if not _can_hide_comment(request, comment):
        raise PermissionDenied()

    # Get IDs of all comments being hidden before the update
    hidden_comment_ids = list(
        comment.get_descendants(include_self=True).values_list("id", flat=True)
    )
    comment.get_descendants(include_self=True).update(hidden=True)

    # Dirty comment count caches
    get_visible_comment_count.dirty(comment.content_type_id, comment.object_id)
    get_visible_top_level_comment_count.dirty(
        comment.content_type_id, comment.object_id
    )

    # Dirty individual comment caches
    Comment.dirty_cache(*hidden_comment_ids)

    # Dirty list caches
    Comment.dirty_list_cache(
        comment.content_type_id, comment.object_id, comment.parent_id
    )

    # Log moderation action if comment is on an organization blog post
    blog_content_type = ContentType.objects.get_for_model(BlogPost)
    if comment.content_type == blog_content_type:
        try:
            blog = BlogPost.objects.get(id=comment.object_id)
            if blog.is_organization_private:
                for org in blog.organizations.all():
                    OrganizationModerationLog.log_action(
                        organization=org,
                        content_object=comment,
                        action="hide_comment",
                        moderator=request.profile,
                    )
        except BlogPost.DoesNotExist:
            pass

    return HttpResponse("ok")


@ratelimit(key="user", rate=settings.RL_COMMENT)
@login_required
@require_POST
def post_comment(request):
    """
    Endpoint for posting a comment via AJAX.

    Expected POST parameters:
    - parent: ID of parent comment (optional)
    - content_type_id: ContentType ID of the object being commented on
    - object_id: ID of the object being commented on
    - body: Comment text content
    """
    if is_comment_locked(request):
        return HttpResponseForbidden("Comments are locked in this contest")

    if (
        not request.user.is_staff
        and not request.profile.submission_set.filter(
            points=F("problem__points")
        ).exists()
    ):
        return HttpResponseBadRequest(
            "You need to solve at least one problem before commenting"
        )

    parent = request.POST.get("parent")
    content_type_id = request.POST.get("content_type_id")
    object_id = request.POST.get("object_id")

    try:
        content_type = ContentType.objects.get(id=int(content_type_id))
        object_id = int(object_id)
    except (ValueError, ContentType.DoesNotExist):
        return HttpResponseBadRequest("Invalid content type or object ID")

    try:
        model_class = content_type.model_class()
        target_object = model_class.objects.get(id=object_id)
    except model_class.DoesNotExist:
        return HttpResponseBadRequest("Target object does not exist")

    if parent:
        try:
            parent = int(parent)
            if not Comment.objects.filter(
                content_type=content_type, object_id=object_id, hidden=False, id=parent
            ).exists():
                return HttpResponseNotFound("Parent comment not found")
        except ValueError:
            return HttpResponseBadRequest("Invalid parent comment ID")

    form = CommentForm(request, request.POST)
    if not form.is_valid():
        errors = {}
        for field, error_list in form.errors.items():
            errors[field] = [str(error) for error in error_list]
        return HttpResponseBadRequest(
            json.dumps(errors), content_type="application/json"
        )

    # Save the comment
    comment = form.save(commit=False)
    comment.author = request.profile
    comment.content_type = content_type
    comment.object_id = object_id

    with revisions.create_revision():
        revisions.set_user(request.user)
        revisions.set_comment(_("Posted comment"))
        comment.save()

    comment_notif_link = get_html_link_notification(comment)

    # Notify parent comment author if replying
    if comment.parent and comment.parent.author != comment.author:
        Notification.objects.bulk_create_notifications(
            user_ids=[comment.parent.author_id],
            category=NotificationCategory.REPLY,
            html_link=comment_notif_link,
            author=comment.author,
        )

    # Notify page authors if applicable
    if hasattr(target_object, "authors"):
        page_authors = list(target_object.authors.values_list("id", flat=True))
        Notification.objects.bulk_create_notifications(
            user_ids=page_authors,
            category=NotificationCategory.COMMENT,
            html_link=comment_notif_link,
            author=comment.author,
        )

    add_mention_notifications(comment)

    # Note: save() override in Comment model also dirties cache on new comments,
    # but we keep this explicit call for clarity
    get_visible_comment_count.dirty(comment.content_type_id, comment.object_id)
    get_visible_top_level_comment_count.dirty(
        comment.content_type_id, comment.object_id
    )

    # Refresh comment to get cached properties
    comment = Comment(id=comment.id)
    # Set vote_score for template (new comment has no votes yet)
    comment.vote_score = 0

    return render(
        request,
        "comments/content.html",
        {
            "comment": comment,
            "profile": request.profile,
            "vote_hide_threshold": settings.DMOJ_COMMENT_VOTE_HIDE_THRESHOLD,
            "perms": PermWrapper(request.user),
        },
    )
