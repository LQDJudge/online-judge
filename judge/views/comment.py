from itertools import chain
import json

from django import forms
from django.conf import settings
from django.contrib.auth.context_processors import PermWrapper
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.db.models import Count, F, FilteredRelation, Q, Case, When
from django.db.models.expressions import Value
from django.db.models.functions import Coalesce
from django.forms import ModelForm
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotFound,
    HttpResponseRedirect,
)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.utils.datastructures import MultiValueDictKeyError
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, UpdateView, View
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin
from django_ratelimit.decorators import ratelimit
from django.contrib.contenttypes.models import ContentType

from reversion import revisions
from reversion.models import Revision, Version

from judge.jinja2.reference import get_user_from_text
from judge.models import Comment, CommentVote
from judge.models.notification import make_notification
from judge.models.comment import (
    get_visible_comment_count,
    get_visible_top_level_comment_count,
)
from judge.utils.views import TitleMixin
from judge.widgets import HeavyPreviewPageDownWidget

__all__ = [
    "upvote_comment",
    "downvote_comment",
    "CommentEditAjax",
    "CommentContent",
    "CommentEdit",
    "get_comments",
    "get_replies",
    "comment_hide",
    "post_comment",
    "CommentableMixin",
]

DEFAULT_COMMENT_LIMIT = 10


def _get_html_link_notification(comment):
    return f'<a href="{comment.get_absolute_url()}">{comment.page_title}</a>'


def add_mention_notifications(comment):
    users_mentioned = (
        get_user_from_text(comment.body)
        .exclude(id=comment.author.id)
        .values_list("id", flat=True)
    )
    link = _get_html_link_notification(comment)
    make_notification(list(users_mentioned), "Mention", link, comment.author)


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
    return HttpResponse("success", content_type="text/plain")


def upvote_comment(request):
    return vote_comment(request, 1)


def downvote_comment(request):
    return vote_comment(request, -1)


def get_comments_data(request, limit=DEFAULT_COMMENT_LIMIT):
    """
    Common function to fetch comments data for both the API endpoint and the mixin.

    Returns a tuple of:
    - comments_qs: QuerySet of comments
    - context: Dict with render context
    - error: HttpResponse with error or None
    """
    try:
        # Check if we're fetching top-level comments (is_top_level=1) or replies (is_top_level=0)
        is_top_level = int(request.GET.get("is_top_level", "1"))

        # Parse sorting parameters
        sort_by = request.GET.get("sort_by", "time")  # Default: sort by time
        sort_order = request.GET.get("sort_order", "desc")  # Default: newest first

        # Validate sort parameters
        if sort_by not in ["time", "score"]:
            sort_by = "time"
        if sort_order not in ["asc", "desc"]:
            sort_order = "desc"

        # Determine if we're fetching replies to a comment or comments for an object
        if "id" in request.GET:
            # Fetching replies to a comment
            source_id = int(request.GET["id"])
            if not Comment.objects.filter(id=source_id).exists():
                return None, None, Http404()
            source_comment = Comment.objects.get(pk=source_id)
            comment_root_id = source_comment.id

            # Get the comments based on parent relationship
            if is_top_level:
                comments_qs = source_comment.linked_object.comments.filter(
                    parent=None, hidden=False
                )
            else:
                comments_qs = source_comment.linked_object.comments.filter(
                    parent=source_comment, hidden=False
                )
        elif "content_type_id" in request.GET and "object_id" in request.GET:
            # Fetching comments for a specific object using ContentType
            content_type_id = int(request.GET["content_type_id"])
            object_id = int(request.GET["object_id"])

            # Get the content type and verify it exists
            try:
                content_type = ContentType.objects.get(id=content_type_id)
                model_class = content_type.model_class()
                source_object = model_class.objects.get(id=object_id)
                comment_root_id = 0
                source_comment = None

                # Check if we're highlighting a specific comment
                target_comment_id = request.GET.get("target_comment", -1)
                try:
                    target_comment_id = int(target_comment_id)
                    if target_comment_id > 0:
                        # Get the target comment to highlight
                        target_comment = Comment.objects.get(
                            id=target_comment_id,
                            content_type=content_type,
                            object_id=object_id,
                        )

                        # Get top-level comments, but prioritize the comment tree containing the target
                        comments_qs = Comment.objects.filter(
                            content_type=content_type,
                            object_id=object_id,
                            parent=None,
                            hidden=False,
                        )
                    else:
                        # Normal case - just get all top-level comments
                        comments_qs = Comment.objects.filter(
                            content_type=content_type,
                            object_id=object_id,
                            parent=None,
                            hidden=False,
                        )
                except (ValueError, Comment.DoesNotExist):
                    # Default to all top-level comments if target_comment is invalid
                    comments_qs = Comment.objects.filter(
                        content_type=content_type,
                        object_id=object_id,
                        parent=None,
                        hidden=False,
                    )
            except (ContentType.DoesNotExist, model_class.DoesNotExist):
                return None, None, Http404()
        else:
            return None, None, HttpResponseBadRequest("Missing required parameters")
    except (ValueError, MultiValueDictKeyError):
        return None, None, HttpResponseBadRequest()

    # Handle pagination
    page_offset = 0
    if "offset" in request.GET:
        try:
            page_offset = int(request.GET["offset"])
        except ValueError:
            return None, None, HttpResponseBadRequest()

    # Handle highlighted comment
    target_comment_id = -1
    if "target_comment" in request.GET:
        try:
            target_comment_id = int(request.GET["target_comment"])
        except ValueError:
            return None, None, HttpResponseBadRequest()

    # Check if we need to prioritize a specific comment tree
    # Only do this for the initial request (offset=0), not for pagination (show_more)
    root_comment = None
    if (
        target_comment_id > 0
        and page_offset == 0
        and "content_type_id" in request.GET
        and "object_id" in request.GET
    ):
        try:
            target_comment = Comment.objects.get(id=target_comment_id)

            root_comment = target_comment.get_root()

            if not (
                root_comment.content_type_id == int(request.GET["content_type_id"])
                and root_comment.object_id == int(request.GET["object_id"])
            ):
                root_comment = None
        except (Comment.DoesNotExist, ValueError):
            root_comment = None

    total_comments = comments_qs.count()

    comments_qs = comments_qs.annotate(
        count_replies=Count("replies", distinct=True),
    )

    if request.user.is_authenticated:
        comments_qs = comments_qs.annotate(
            my_vote=FilteredRelation(
                "votes", condition=Q(votes__voter_id=request.profile.id)
            ),
        ).annotate(vote_score=Coalesce(F("my_vote__score"), Value(0)))

    # Apply sorting
    if sort_by == "score":
        if sort_order == "desc":
            comments_qs = comments_qs.order_by("-score", "-time")
        else:
            comments_qs = comments_qs.order_by("score", "-time")
    else:  # sort_by == "time"
        if sort_order == "desc":
            comments_qs = comments_qs.order_by("-time")
        else:
            comments_qs = comments_qs.order_by("time")

    comments_qs = comments_qs[page_offset : page_offset + limit]

    comments_list = list(comments_qs)

    if root_comment is not None:
        root_tree = root_comment.get_descendants(include_self=True)
        root_tree = root_tree.annotate(
            count_replies=Count("replies", distinct=True),
        )
        if request.user.is_authenticated:
            root_tree = root_tree.annotate(
                my_vote=FilteredRelation(
                    "votes", condition=Q(votes__voter_id=request.profile.id)
                ),
            ).annotate(vote_score=Coalesce(F("my_vote__score"), Value(0)))
        comments_list = [
            comment for comment in comments_list if comment.id != target_comment_id
        ]
        comments_list = list(root_tree) + comments_list
        total_comments += 1

    comments_qs = comments_list

    next_page_offset = page_offset + min(len(comments_qs), limit)

    context = {
        "profile": request.profile,
        "comment_root_id": comment_root_id,
        "comment_list": comments_qs,
        "vote_hide_threshold": settings.DMOJ_COMMENT_VOTE_HIDE_THRESHOLD,
        "perms": PermWrapper(request.user),
        "offset": next_page_offset,
        "limit": limit,
        "comment_count": total_comments,
        "is_top_level": is_top_level,
        "target_comment": target_comment_id,
        "comment_more": total_comments - next_page_offset,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }

    return comments_qs, context, None


def get_comments(request, limit=DEFAULT_COMMENT_LIMIT):
    """
    Get comments for a specific object or fetch replies for a specific comment.

    This function works in two modes:
    1. When content_type and object_id are provided, it fetches top-level comments for an object
    2. When id (comment_id) is provided, it fetches replies to that comment

    Parameters from request.GET:
    - content_type_id: ContentType ID of the object being commented on (optional)
    - object_id: ID of the object being commented on (optional)
    - id: ID of the comment to get replies for (optional)
    - is_top_level: 1 if fetching top-level comments, 0 if fetching replies
    - offset: Pagination offset (optional)
    - target_comment: ID of a specific comment to highlight (optional)
    """
    comments_qs, context, error = get_comments_data(request, limit)
    if error:
        return error

    return render(request, "comments/content-list.html", context)


def get_replies(request):
    """
    Fetch replies for a specific comment, an alias for get_comments.
    """
    return get_comments(request)


class CommentMixin(object):
    model = Comment
    pk_url_kwarg = "id"
    context_object_name = "comment"


class CommentRevisionAjax(CommentMixin, DetailView):
    template_name = "comments/revision-ajax.html"

    def get_context_data(self, **kwargs):
        context = super(CommentRevisionAjax, self).get_context_data(**kwargs)
        revisions = Version.objects.get_for_object(self.object).order_by("-revision")

        if len(revisions) == 0:
            raise Http404

        try:
            wanted = min(
                max(int(self.request.GET.get("revision", 0)), 0), len(revisions) - 1
            )
            revision = revisions[wanted]
        except (ValueError, IndexError):
            raise Http404

        data = json.loads(revision.serialized_data)
        try:
            context["body"] = data[0]["fields"]["body"]
        except Exception:
            context["body"] = ""
        return context

    def get_object(self, queryset=None):
        comment = super(CommentRevisionAjax, self).get_object(queryset)
        if comment.hidden and not self.request.user.has_perm("judge.change_comment"):
            raise Http404()
        return comment


class CommentEditForm(ModelForm):
    class Meta:
        model = Comment
        fields = ["body"]
        widgets = {
            "body": HeavyPreviewPageDownWidget(
                id="id-edit-comment-body",
                preview=reverse_lazy("comment_preview"),
                preview_timeout=1000,
                hide_preview_button=True,
            ),
        }


class CommentEditAjax(LoginRequiredMixin, CommentMixin, UpdateView):
    template_name = "comments/edit-ajax.html"
    form_class = CommentEditForm

    def form_valid(self, form):
        # update notifications
        comment = form.instance
        add_mention_notifications(comment)
        comment.revision_count = comment.versions.count() + 1
        comment.save(update_fields=["revision_count"])
        with revisions.create_revision():
            revisions.set_comment(_("Edited from site"))
            revisions.set_user(self.request.user)
            return super(CommentEditAjax, self).form_valid(form)

    def get_success_url(self):
        return self.object.get_absolute_url()

    def get_object(self, queryset=None):
        comment = super(CommentEditAjax, self).get_object(queryset)
        if self.request.user.has_perm("judge.change_comment"):
            return comment
        profile = self.request.profile
        if profile != comment.author or profile.mute or comment.hidden:
            raise Http404()
        return comment


class CommentEdit(TitleMixin, CommentEditAjax):
    template_name = "comments/edit.html"

    def get_title(self):
        return _("Editing comment")


class CommentContent(CommentMixin, DetailView):
    template_name = "comments/content.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["vote_hide_threshold"] = settings.DMOJ_COMMENT_VOTE_HIDE_THRESHOLD
        return context


class CommentVotesAjax(PermissionRequiredMixin, CommentMixin, DetailView):
    template_name = "comments/votes.html"
    permission_required = "judge.change_commentvote"

    def get_context_data(self, **kwargs):
        context = super(CommentVotesAjax, self).get_context_data(**kwargs)
        context["votes"] = self.object.votes.select_related("voter__user").only(
            "id", "voter__display_rank", "voter__user__username", "score"
        )
        return context


@require_POST
def comment_hide(request):
    if not request.user.has_perm("judge.change_comment"):
        raise PermissionDenied()

    try:
        comment_id = int(request.POST["id"])
    except ValueError:
        return HttpResponseBadRequest()

    comment = get_object_or_404(Comment, id=comment_id)
    comment.get_descendants(include_self=True).update(hidden=True)

    get_visible_comment_count.dirty(comment.content_type, comment.object_id)
    get_visible_top_level_comment_count.dirty(comment.content_type, comment.object_id)

    return HttpResponse("ok")


def is_comment_locked(request):
    """
    Check if comments are locked for the current user.

    Returns True if comments are locked, False otherwise.
    """
    if request.user.has_perm("judge.override_comment_lock"):
        return False
    return request.in_contest and request.participation.contest.use_clarifications


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
    # Check if user is allowed to comment (not in a locked contest)
    if is_comment_locked(request):
        return HttpResponseForbidden("Comments are locked in this contest")

    # Check if user has solved at least one problem
    if (
        not request.user.is_staff
        and not request.profile.submission_set.filter(
            points=F("problem__points")
        ).exists()
    ):
        return HttpResponseBadRequest(
            "You need to solve at least one problem before commenting"
        )

    # Get and validate parent comment if provided
    parent = request.POST.get("parent")
    content_type_id = request.POST.get("content_type_id")
    object_id = request.POST.get("object_id")

    try:
        content_type = ContentType.objects.get(id=int(content_type_id))
        object_id = int(object_id)
    except (ValueError, ContentType.DoesNotExist):
        return HttpResponseBadRequest("Invalid content type or object ID")

    # Try to get the target object
    try:
        model_class = content_type.model_class()
        target_object = model_class.objects.get(id=object_id)
    except model_class.DoesNotExist:
        return HttpResponseBadRequest("Target object does not exist")

    # Check parent comment if specified
    if parent:
        try:
            parent = int(parent)
            if not Comment.objects.filter(
                content_type=content_type, object_id=object_id, hidden=False, id=parent
            ).exists():
                return HttpResponseNotFound("Parent comment not found")
        except ValueError:
            return HttpResponseBadRequest("Invalid parent comment ID")

    # Create and validate the comment
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

    # Add notifications
    comment_notif_link = _get_html_link_notification(comment)

    # Notify parent comment author if replying
    if comment.parent and comment.parent.author != comment.author:
        make_notification(
            [comment.parent.author_id], "Reply", comment_notif_link, comment.author
        )

    # Notify page authors if applicable
    if hasattr(target_object, "authors"):
        page_authors = list(target_object.authors.values_list("id", flat=True))
        make_notification(page_authors, "Comment", comment_notif_link, comment.author)

    # Add mention notifications
    add_mention_notifications(comment)

    # Update comment count cache
    get_visible_comment_count.dirty(comment.content_type, comment.object_id)
    get_visible_top_level_comment_count.dirty(comment.content_type, comment.object_id)

    # Return the newly created comment HTML for insertion
    # First, annotate with vote score for the current user
    comment = (
        Comment.objects.filter(id=comment.id)
        .annotate(
            count_replies=Count("replies", distinct=True),
            my_vote=FilteredRelation(
                "votes", condition=Q(votes__voter_id=request.profile.id)
            ),
        )
        .annotate(vote_score=Coalesce(F("my_vote__score"), Value(0)))
        .first()
    )

    # Render the comment as HTML for insertion
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


class CommentForm(ModelForm):
    class Meta:
        model = Comment
        fields = ["body", "parent"]
        widgets = {
            "parent": forms.HiddenInput(),
        }

        if HeavyPreviewPageDownWidget is not None:
            widgets["body"] = HeavyPreviewPageDownWidget(
                preview=reverse_lazy("comment_preview"),
                preview_timeout=1000,
                hide_preview_button=True,
            )

    def __init__(self, request, *args, **kwargs):
        self.request = request
        super(CommentForm, self).__init__(*args, **kwargs)
        self.fields["body"].widget.attrs.update({"placeholder": _("Comment body")})

    def clean(self):
        if self.request is not None and self.request.user.is_authenticated:
            profile = self.request.profile
            if profile.mute:
                raise ValidationError(_("Your part is silent, little toad."))
            elif (
                not self.request.user.is_staff
                and not profile.submission_set.filter(
                    points=F("problem__points")
                ).exists()
            ):
                raise ValidationError(
                    _(
                        "You need to have solved at least one problem "
                        "before your voice can be heard."
                    )
                )
        return super(CommentForm, self).clean()


class CommentableMixin:
    """
    Mixin for views that include comments.

    Provides the necessary context for AJAX-loaded comments and comment form,
    but does not render comments directly.
    """

    def get_comment_context(self, context=None):
        """
        Add comment-related context data.

        Can be called from get_context_data() in the implementing view.
        """
        if context is None:
            context = {}

        # Get metadata about comments for this object
        content_type = ContentType.objects.get_for_model(self.object)
        object_id = self.object.pk

        total_comment_count = get_visible_comment_count(content_type, object_id)
        top_level_count = get_visible_top_level_comment_count(content_type, object_id)

        # Check if user is new (hasn't solved any problems)
        if self.request.user.is_authenticated:
            context["is_new_user"] = (
                not self.request.user.is_staff
                and not self.request.profile.submission_set.filter(
                    points=F("problem__points")
                ).exists()
            )

        # Get sort parameters from request or use defaults
        sort_by = self.request.GET.get("sort_by", "time")
        sort_order = self.request.GET.get("sort_order", "desc")

        # Validate sort parameters
        if sort_by not in ["time", "score"]:
            sort_by = "time"
        if sort_order not in ["asc", "desc"]:
            sort_order = "desc"

        # Basic comment configuration
        context.update(
            {
                "comment_lock": is_comment_locked(self.request),
                "has_comments": top_level_count > 0,
                "all_comment_count": total_comment_count,
                "comment_count": top_level_count,
                "vote_hide_threshold": settings.DMOJ_COMMENT_VOTE_HIDE_THRESHOLD,
                "comment_content_type_id": content_type.id,
                "comment_object_id": object_id,
                "limit": DEFAULT_COMMENT_LIMIT,
                "is_top_level": 1,
                "sort_by": sort_by,
                "sort_order": sort_order,
            }
        )

        # Check for either parameter name for backward compatibility
        target_comment_id = None
        if "target_comment" in self.request.GET:
            target_comment_id = self.request.GET["target_comment"]

        if target_comment_id:
            try:
                comment_id = int(target_comment_id)
                comment_obj = Comment.objects.get(id=comment_id)
                if (
                    comment_obj.content_type != content_type
                    or comment_obj.object_id != object_id
                ):
                    raise Http404
                context["initial_comment_id"] = comment_id
                context["target_comment"] = comment_obj.get_root().id
            except (ValueError, Comment.DoesNotExist):
                context["target_comment"] = -1
        else:
            context["target_comment"] = -1

        context["comment_form"] = CommentForm(self.request, initial={"parent": None})

        return context
