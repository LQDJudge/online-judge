import json

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
    HttpResponseRedirect,
)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, UpdateView, View
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin
from django_ratelimit.decorators import ratelimit
from django.contrib.contenttypes.models import ContentType

from reversion import revisions
from reversion.models import Revision, Version

from judge.jinja2.reference import get_user_from_text
from judge.models import BlogPost, Comment, CommentVote, Notification
from judge.models.notification import make_notification
from judge.models.comment import get_visible_comment_count
from judge.utils.views import TitleMixin
from judge.widgets import HeavyPreviewPageDownWidget

__all__ = [
    "upvote_comment",
    "downvote_comment",
    "CommentEditAjax",
    "CommentContent",
    "CommentEdit",
]

DEFAULT_OFFSET = 10


def _get_html_link_notification(comment):
    return f'<a href="{comment.get_absolute_url()}">{comment.page_title}</a>'


def add_mention_notifications(comment):
    users_mentioned = get_user_from_text(comment.body).exclude(id=comment.author.id)
    link = _get_html_link_notification(comment)
    make_notification(users_mentioned, "Mention", link, comment.author)


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


def get_comments(request, limit=10):
    try:
        comment_id = int(request.GET["id"])
        parent_none = int(request.GET["parent_none"])
    except ValueError:
        return HttpResponseBadRequest()
    else:
        if comment_id and not Comment.objects.filter(id=comment_id).exists():
            raise Http404()

    offset = 0
    if "offset" in request.GET:
        offset = int(request.GET["offset"])

    target_comment = -1
    if "target_comment" in request.GET:
        target_comment = int(request.GET["target_comment"])

    comment_root_id = 0

    if comment_id:
        comment_obj = Comment.objects.get(pk=comment_id)
        comment_root_id = comment_obj.id
    else:
        comment_obj = None

    queryset = comment_obj.linked_object.comments
    if parent_none:
        queryset = queryset.filter(parent=None, hidden=False)
        queryset = queryset.exclude(pk=target_comment)
    else:
        queryset = queryset.filter(parent=comment_obj, hidden=False)
    comment_count = len(queryset)
    queryset = (
        queryset.select_related("author__user")
        .defer("author__about")
        .annotate(
            count_replies=Count("replies", distinct=True),
        )[offset : offset + limit]
    )
    profile = None
    if request.user.is_authenticated:
        profile = request.profile
        queryset = queryset.annotate(
            my_vote=FilteredRelation("votes", condition=Q(votes__voter_id=profile.id)),
        ).annotate(vote_score=Coalesce(F("my_vote__score"), Value(0)))

    new_offset = offset + min(len(queryset), limit)

    return render(
        request,
        "comments/content-list.html",
        {
            "profile": profile,
            "comment_root_id": comment_root_id,
            "comment_list": queryset,
            "vote_hide_threshold": settings.DMOJ_COMMENT_VOTE_HIDE_THRESHOLD,
            "perms": PermWrapper(request.user),
            "offset": new_offset,
            "limit": limit,
            "comment_count": comment_count,
            "comment_parent_none": parent_none,
            "target_comment": target_comment,
            "comment_more": comment_count - new_offset,
        },
    )


def get_show_more(request):
    return get_comments(request)


def get_replies(request):
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
        try:
            wanted = min(
                max(int(self.request.GET.get("revision", 0)), 0), len(revisions) - 1
            )
        except ValueError:
            raise Http404
        revision = revisions[wanted]
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
    return HttpResponse("ok")


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


class CommentedDetailView(TemplateResponseMixin, SingleObjectMixin, View):
    comment_page = None

    def is_comment_locked(self):
        if self.request.user.has_perm("judge.override_comment_lock"):
            return False
        return (
            self.request.in_contest
            and self.request.participation.contest.use_clarifications
        )

    @method_decorator(ratelimit(key="user", rate=settings.RL_COMMENT))
    @method_decorator(login_required)
    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.is_comment_locked():
            return HttpResponseForbidden()

        parent = request.POST.get("parent")
        if parent:
            try:
                parent = int(parent)
            except ValueError:
                return HttpResponseNotFound()
            else:
                if not self.object.comments.filter(hidden=False, id=parent).exists():
                    return HttpResponseNotFound()

        form = CommentForm(request, request.POST)
        if form.is_valid():
            comment = form.save(commit=False)
            comment.author = request.profile
            comment.linked_object = self.object

            with revisions.create_revision():
                revisions.set_user(request.user)
                revisions.set_comment(_("Posted comment"))
                comment.save()

            # add notification for reply
            comment_notif_link = _get_html_link_notification(comment)
            if comment.parent and comment.parent.author != comment.author:
                make_notification(
                    [comment.parent.author], "Reply", comment_notif_link, comment.author
                )

            # add notification for page authors
            page_authors = comment.linked_object.authors.all()
            make_notification(
                page_authors, "Comment", comment_notif_link, comment.author
            )

            add_mention_notifications(comment)
            get_visible_comment_count.dirty(comment.content_type, comment.object_id)

            return HttpResponseRedirect(comment.get_absolute_url())

        context = self.get_context_data(object=self.object, comment_form=form)
        return self.render_to_response(context)

    def get(self, request, *args, **kwargs):
        target_comment = None
        self.object = self.get_object()
        if "comment-id" in request.GET:
            try:
                comment_id = int(request.GET["comment-id"])
                comment_obj = Comment.objects.get(id=comment_id)
            except (Comment.DoesNotExist, ValueError):
                raise Http404
            if comment_obj.linked_object != self.object:
                raise Http404
            target_comment = comment_obj.get_root()
        return self.render_to_response(
            self.get_context_data(
                object=self.object,
                target_comment=target_comment,
                comment_form=CommentForm(request, initial={"parent": None}),
            )
        )

    def _get_queryset(self, target_comment):
        if target_comment:
            queryset = target_comment.get_descendants(include_self=True)
            queryset = (
                queryset.select_related("author__user")
                .filter(hidden=False)
                .defer("author__about")
            )
        else:
            queryset = self.object.comments
            queryset = queryset.filter(parent=None, hidden=False)
            queryset = (
                queryset.select_related("author__user")
                .defer("author__about")
                .filter(hidden=False)
                .annotate(
                    count_replies=Count("replies", distinct=True),
                )[:DEFAULT_OFFSET]
            )

        if self.request.user.is_authenticated:
            profile = self.request.profile
            queryset = queryset.annotate(
                my_vote=FilteredRelation(
                    "votes", condition=Q(votes__voter_id=profile.id)
                ),
            ).annotate(vote_score=Coalesce(F("my_vote__score"), Value(0)))

        return queryset

    def get_context_data(self, target_comment=None, **kwargs):
        context = super(CommentedDetailView, self).get_context_data(**kwargs)
        queryset = self._get_queryset(target_comment)
        comment_count = self.object.comments.filter(parent=None, hidden=False).count()

        content_type = ContentType.objects.get_for_model(self.object)
        all_comment_count = get_visible_comment_count(content_type, self.object.pk)

        if target_comment != None:
            context["target_comment"] = target_comment.id
        else:
            context["target_comment"] = -1

        if self.request.user.is_authenticated:
            context["is_new_user"] = (
                not self.request.user.is_staff
                and not self.request.profile.submission_set.filter(
                    points=F("problem__points")
                ).exists()
            )

        context["has_comments"] = queryset.exists()
        context["comment_lock"] = self.is_comment_locked()
        context["comment_list"] = list(queryset)

        context["vote_hide_threshold"] = settings.DMOJ_COMMENT_VOTE_HIDE_THRESHOLD

        if queryset.exists():
            context["comment_root_id"] = context["comment_list"][0].id
        else:
            context["comment_root_id"] = 0

        context["comment_parent_none"] = 1

        if target_comment != None:
            context["offset"] = 0
            context["comment_more"] = comment_count - 1
        else:
            context["offset"] = DEFAULT_OFFSET
            context["comment_more"] = comment_count - DEFAULT_OFFSET

        context["limit"] = DEFAULT_OFFSET
        context["comment_count"] = comment_count
        context["profile"] = self.request.profile
        context["all_comment_count"] = all_comment_count

        return context
