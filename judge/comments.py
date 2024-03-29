from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db.models import Count, FilteredRelation, Q
from django.db.models.expressions import F, Value
from django.db.models.functions import Coalesce
from django.forms import ModelForm
from django.http import (
    HttpResponseForbidden,
    HttpResponseNotFound,
    HttpResponseRedirect,
    Http404,
)
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views.generic import View
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin
from reversion import revisions
from reversion.models import Revision, Version
from django_ratelimit.decorators import ratelimit

from judge.models import Comment, Notification
from judge.widgets import HeavyPreviewPageDownWidget
from judge.jinja2.reference import get_user_from_text
from judge.models.notification import make_notification


DEFAULT_OFFSET = 10


def _get_html_link_notification(comment):
    return f'<a href="{comment.get_absolute_url()}">{comment.page_title}</a>'


def add_mention_notifications(comment):
    users_mentioned = get_user_from_text(comment.body).exclude(id=comment.author.id)
    link = _get_html_link_notification(comment)
    make_notification(users_mentioned, "Mention", link, comment.author)


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
        context["target_comment"] = -1
        if target_comment != None:
            context["target_comment"] = target_comment.id

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
        return context
