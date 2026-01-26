from dataclasses import dataclass
import json
from typing import Optional

from django import forms
from django.conf import settings
from django.contrib.auth.context_processors import PermWrapper
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.db.models import Count, F, FilteredRelation, Q
from django.db.models.expressions import Value
from django.db.models.functions import Coalesce
from django.forms import ModelForm
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotFound,
)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse_lazy
from django.utils.translation import gettext as _
from django.utils.datastructures import MultiValueDictKeyError
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView, UpdateView

from reversion import revisions
from reversion.models import Version

from judge.jinja2.reference import get_user_from_text
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
from judge.utils.views import TitleMixin
from judge.utils.ratelimit import ratelimit
from judge.widgets import HeavyPreviewPageDownWidget
from judge.views.feed import HomeFeedView

__all__ = [
    "upvote_comment",
    "downvote_comment",
    "CommentEditAjax",
    "CommentContent",
    "CommentEdit",
    "TopLevelCommentsView",
    "RepliesView",
    "comment_hide",
    "post_comment",
    "CommentableMixin",
    "CommentFeed",
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
    Notification.objects.bulk_create_notifications(
        user_ids=list(users_mentioned),
        category=NotificationCategory.MENTION,
        html_link=link,
        author=comment.author,
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
    return HttpResponse("success", content_type="text/plain")


def upvote_comment(request):
    return vote_comment(request, 1)


def downvote_comment(request):
    return vote_comment(request, -1)


def annotate_comments_for_display(queryset, user):
    """
    Apply standard display annotations to a comment queryset.
    Adds: count_replies, vote_score (if authenticated)
    """
    queryset = queryset.annotate(
        count_replies=Count("replies", distinct=True, filter=Q(replies__hidden=False)),
    )
    if user.is_authenticated:
        queryset = queryset.annotate(
            my_vote=FilteredRelation(
                "votes", condition=Q(votes__voter_id=user.profile.id)
            ),
        ).annotate(vote_score=Coalesce(F("my_vote__score"), Value(0)))
    return queryset


def _parse_sort_params(request, default_order="desc"):
    sort_by = request.GET.get("sort_by", "time")
    if sort_by not in ["time", "score"]:
        sort_by = "time"

    sort_order = request.GET.get("sort_order", default_order)
    if sort_order not in ["asc", "desc"]:
        sort_order = default_order

    return sort_by, sort_order


@dataclass
class CommentParams:
    sort_by: str
    sort_order: str
    offset: int
    target_comment_id: int
    content_type_id: Optional[int]
    object_id: Optional[int]


def _parse_comment_params(request):
    try:
        sort_by, sort_order = _parse_sort_params(request)
        offset = int(request.GET.get("offset", 0))
        target_comment_id = int(request.GET.get("target_comment", -1))

        content_type_id = request.GET.get("content_type_id")
        if content_type_id is not None:
            content_type_id = int(content_type_id)

        object_id = request.GET.get("object_id")
        if object_id is not None:
            object_id = int(object_id)

        return (
            CommentParams(
                sort_by=sort_by,
                sort_order=sort_order,
                offset=offset,
                target_comment_id=target_comment_id,
                content_type_id=content_type_id,
                object_id=object_id,
            ),
            None,
        )
    except (ValueError, MultiValueDictKeyError):
        return None, HttpResponseBadRequest()


def _apply_sorting(queryset, sort_by, sort_order):
    if sort_by == "score":
        if sort_order == "desc":
            return queryset.order_by("-score", "-time")
        return queryset.order_by("score", "-time")
    if sort_order == "desc":
        return queryset.order_by("-time")
    return queryset.order_by("time")


def _get_highlighted_root_tree(target_comment_id, content_type_id, object_id, user):
    """
    Get the root comment tree for a highlighted comment.
    Returns annotated queryset of the root tree or None.
    """
    try:
        target_comment = Comment.objects.get(id=target_comment_id)
        root_comment = target_comment.get_root()

        if not (
            root_comment.content_type_id == content_type_id
            and root_comment.object_id == object_id
        ):
            return None

        root_tree = root_comment.get_descendants(include_self=True)
        return annotate_comments_for_display(root_tree, user)
    except (Comment.DoesNotExist, ValueError):
        return None


COMPACT_COMMENT_LIMIT = 3


class CommentListView(ListView):
    template_name = "comments/content-list.html"
    context_object_name = "comment_list"
    limit = DEFAULT_COMMENT_LIMIT
    error_response = None

    def get_comment_root_id(self):
        raise NotImplementedError

    def get_target_comment_id(self):
        return -1

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.sort_by, self.sort_order = _parse_sort_params(request)
        self.compact = request.GET.get("compact") == "1"
        if self.compact:
            self.limit = COMPACT_COMMENT_LIMIT
            self.template_name = "comments/inline-comments.html"
        try:
            self.offset = int(request.GET.get("offset", 0))
        except ValueError:
            self.offset = 0

    def get(self, request, *args, **kwargs):
        self.object_list = self.get_queryset()
        if self.error_response:
            return self.error_response
        context = self.get_context_data()
        return self.render_to_response(context)

    def get_queryset(self):
        raise NotImplementedError

    def post_process_comments(self, comments_list, total_comments):
        return comments_list, total_comments

    def get_context_data(self, **kwargs):
        queryset = self.object_list
        self.total_comments = queryset.count()

        queryset = annotate_comments_for_display(queryset, self.request.user)
        queryset = _apply_sorting(queryset, self.sort_by, self.sort_order)
        queryset = queryset[self.offset : self.offset + self.limit]
        comments_list = list(queryset)

        comments_list, self.total_comments = self.post_process_comments(
            comments_list, self.total_comments
        )

        next_page_offset = self.offset + min(len(comments_list), self.limit)

        # Determine if user is new (can't comment)
        is_new_user = False
        if self.request.user.is_authenticated:
            is_new_user = (
                not self.request.user.is_staff
                and not self.request.profile.submission_set.filter(
                    points=F("problem__points")
                ).exists()
            )

        context = super().get_context_data(**kwargs)
        context.update(
            {
                "profile": self.request.profile,
                "comment_root_id": self.get_comment_root_id(),
                "comment_list": comments_list,
                "vote_hide_threshold": settings.DMOJ_COMMENT_VOTE_HIDE_THRESHOLD,
                "offset": next_page_offset,
                "limit": self.limit,
                "comment_count": self.total_comments,
                "target_comment": self.get_target_comment_id(),
                "comment_more": self.total_comments - next_page_offset,
                "sort_by": self.sort_by,
                "sort_order": self.sort_order,
                "compact": getattr(self, "compact", False),
                "comment_lock": is_comment_locked(self.request),
                "is_new_user": is_new_user,
            }
        )
        return context


class TopLevelCommentsView(CommentListView):
    def get_queryset(self):
        params, error = _parse_comment_params(self.request)
        if error:
            self.error_response = error
            return Comment.objects.none()

        self.params = params
        self.offset = params.offset
        self.sort_by = params.sort_by
        self.sort_order = params.sort_order

        if params.content_type_id is None or params.object_id is None:
            self.error_response = HttpResponseBadRequest(
                "Missing content_type_id or object_id"
            )
            return Comment.objects.none()

        try:
            content_type = ContentType.objects.get(id=params.content_type_id)
            model_class = content_type.model_class()
            self.target_object = model_class.objects.get(id=params.object_id)
            self.content_type = content_type
        except (ContentType.DoesNotExist, model_class.DoesNotExist):
            self.error_response = HttpResponseNotFound()
            return Comment.objects.none()

        self.comment_root_id = 0
        return Comment.objects.filter(
            content_type=content_type,
            object_id=params.object_id,
            parent=None,
            hidden=False,
        )

    def get_comment_root_id(self):
        return self.comment_root_id

    def get_target_comment_id(self):
        return self.params.target_comment_id

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if hasattr(self, "params") and hasattr(self, "target_object"):
            context["content_type_id"] = self.params.content_type_id
            context["object_id"] = self.params.object_id
            if hasattr(self.target_object, "get_absolute_url"):
                context["page_url"] = self.target_object.get_absolute_url()
            # Check if user can hide comments on this content
            context["can_hide_comments"] = self._can_hide_comments()
        return context

    def _can_hide_comments(self):
        """Check if user can hide comments on this content."""
        if not self.request.user.is_authenticated:
            return False
        if self.request.user.has_perm("judge.change_comment"):
            return True
        profile = self.request.profile
        if not profile or not hasattr(self, "target_object"):
            return False
        obj = self.target_object
        # Use cached method if available
        if hasattr(obj, "get_author_ids"):
            return profile.id in obj.get_author_ids()
        elif hasattr(obj, "authors"):
            return obj.authors.filter(id=profile.id).exists()
        return False

    def post_process_comments(self, comments_list, total_comments):
        if (
            self.params.target_comment_id > 0
            and self.offset == 0
            and self.params.content_type_id is not None
        ):
            root_tree = _get_highlighted_root_tree(
                self.params.target_comment_id,
                self.params.content_type_id,
                self.params.object_id,
                self.request.user,
            )
            if root_tree is not None:
                comments_list = [
                    c for c in comments_list if c.id != self.params.target_comment_id
                ]
                comments_list = list(root_tree) + comments_list
                total_comments += 1
        return comments_list, total_comments


class RepliesView(CommentListView):
    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.sort_by, self.sort_order = _parse_sort_params(request, default_order="asc")

    def get_queryset(self):
        try:
            self.comment_id = int(self.request.GET.get("id", 0))
        except ValueError:
            self.error_response = HttpResponseBadRequest()
            return Comment.objects.none()

        if not self.comment_id:
            self.error_response = HttpResponseBadRequest("Missing comment id")
            return Comment.objects.none()

        source_comment = Comment.objects.filter(id=self.comment_id).first()
        if not source_comment:
            self.error_response = HttpResponseNotFound()
            return Comment.objects.none()

        return source_comment.linked_object.comments.filter(
            parent=source_comment, hidden=False
        )

    def get_comment_root_id(self):
        return self.comment_id


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
        context["profile"] = self.request.profile
        context["perms"] = PermWrapper(self.request.user)
        context["comment_lock"] = is_comment_locked(self.request)
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
    comment.get_descendants(include_self=True).update(hidden=True)

    get_visible_comment_count.dirty(comment.content_type, comment.object_id)
    get_visible_top_level_comment_count.dirty(comment.content_type, comment.object_id)

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

    comment_notif_link = _get_html_link_notification(comment)

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

    get_visible_comment_count.dirty(comment.content_type, comment.object_id)
    get_visible_top_level_comment_count.dirty(comment.content_type, comment.object_id)

    comment = annotate_comments_for_display(
        Comment.objects.filter(id=comment.id), request.user
    ).first()

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

        if self.request.user.is_authenticated:
            context["is_new_user"] = (
                not self.request.user.is_staff
                and not self.request.profile.submission_set.filter(
                    points=F("problem__points")
                ).exists()
            )

        sort_by, sort_order = _parse_sort_params(self.request)

        target_comment = -1
        target_comment_id = self.request.GET.get("target_comment")
        if target_comment_id:
            try:
                comment_obj = Comment.objects.get(id=int(target_comment_id))
                if (
                    comment_obj.content_type != content_type
                    or comment_obj.object_id != object_id
                ):
                    raise Http404
                target_comment = comment_obj.get_root().id
            except (ValueError, Comment.DoesNotExist):
                pass

        context.update(
            {
                "comment_lock": is_comment_locked(self.request),
                "has_comments": top_level_count > 0,
                "all_comment_count": total_comment_count,
                "comment_content_type_id": content_type.id,
                "comment_object_id": object_id,
                "sort_by": sort_by,
                "sort_order": sort_order,
                "target_comment": target_comment,
                "comment_form": CommentForm(self.request, initial={"parent": None}),
            }
        )

        return context


class CommentFeed(HomeFeedView):
    model = Comment
    context_object_name = "comments"
    paginate_by = 50
    feed_content_template_name = "comments/feed.html"

    def get_queryset(self):
        view_type = self.request.GET.get("view", "all")
        content_filter = self.request.GET.get("content", "all")

        # Overfetch before filtering
        needed_count = min(500, self.page * self.paginate_by * 2)

        return Comment.most_recent(
            user=self.request.user,
            view_type=view_type,
            content_filter=content_filter,
            organization=self.request.organization,
            n=needed_count,
        )

    def get_context_data(self, **kwargs):
        context = super(CommentFeed, self).get_context_data(**kwargs)
        context["title"] = _("Comment feed")
        context["page_type"] = "comment"
        context["view_type"] = self.request.GET.get("view", "all")
        context["content_filter"] = self.request.GET.get("content", "all")

        return context
