import json

from itertools import chain

from django import forms
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import (
    ImproperlyConfigured,
    PermissionDenied,
    ValidationError,
)
from django.db.models import Max, Q
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404
from django.template.defaultfilters import truncatechars
from django.template.loader import get_template
from django.urls import reverse, reverse_lazy
from django.utils.functional import cached_property
from django.utils.html import escape, format_html, linebreaks
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _, gettext_lazy
from django.views import View
from django.views.generic import ListView
from django.views.generic.detail import SingleObjectMixin

from judge import event_poster as event
from judge.models import Problem, Profile, Ticket, TicketMessage, BlogPost
from reversion import revisions
from reversion.models import Version
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.tickets import filter_visible_tickets, own_ticket_filter
from judge.utils.views import SingleObjectFormView, TitleMixin, paginate_query_context
from judge.views.problem import ProblemMixin
from judge.views.feed import HomeFeedView
from judge.widgets import HeavyPreviewPageDownWidget, HeavySelect2MultipleWidget
from judge.models.notification import Notification, NotificationCategory

ticket_widget = (
    forms.Textarea()
    if HeavyPreviewPageDownWidget is None
    else HeavyPreviewPageDownWidget(
        preview=reverse_lazy("ticket_preview"),
        preview_timeout=1000,
        hide_preview_button=True,
    )
)


def add_ticket_notifications(users, author, link, ticket):
    html = f'<a href="{link}">{ticket.linked_item}</a>'
    users = set(users)
    if author in users:
        users.remove(author)
    Notification.objects.bulk_create_notifications(
        user_ids=[u.id for u in users],
        category=NotificationCategory.TICKET,
        html_link=html,
        author=author,
    )


class TicketForm(forms.Form):
    title = forms.CharField(max_length=100, label=gettext_lazy("Ticket title"))
    body = forms.CharField(widget=ticket_widget)

    def __init__(self, request, *args, **kwargs):
        self.request = request
        super(TicketForm, self).__init__(*args, **kwargs)
        self.fields["title"].widget.attrs.update({"placeholder": _("Ticket title")})
        self.fields["body"].widget.attrs.update({"placeholder": _("Issue description")})

    def clean(self):
        if self.request is not None and self.request.user.is_authenticated:
            profile = self.request.profile
            if profile.mute:
                raise ValidationError(_("Your part is silent, little toad."))
        return super(TicketForm, self).clean()


class NewTicketView(LoginRequiredMixin, SingleObjectFormView):
    form_class = TicketForm
    template_name = "ticket/new.html"

    def get_assignees(self):
        return []

    def get_form_kwargs(self):
        kwargs = super(NewTicketView, self).get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def form_valid(self, form):
        with revisions.create_revision():
            ticket = Ticket(user=self.request.profile, title=form.cleaned_data["title"])
            ticket.linked_item = self.object
            ticket.save()
            message = TicketMessage(
                ticket=ticket, user=ticket.user, body=form.cleaned_data["body"]
            )
            message.save()
            ticket.assignees.set(self.get_assignees())

            revisions.set_user(self.request.user)
            revisions.set_comment(f"Created ticket: {ticket.title}")

        link = reverse("ticket", args=[ticket.id])

        add_ticket_notifications(ticket.assignees.all(), ticket.user, link, ticket)

        if event.real:
            event.post(
                "tickets",
                {
                    "type": "new-ticket",
                    "id": ticket.id,
                    "message": message.id,
                    "user": ticket.user_id,
                    "assignees": list(ticket.assignees.values_list("id", flat=True)),
                },
            )
        return HttpResponseRedirect(link)


class NewProblemTicketView(ProblemMixin, TitleMixin, NewTicketView):
    template_name = "ticket/new_problem.html"

    def get_assignees(self):
        return self.object.authors.all()

    def get_title(self):
        return _("New ticket for %s") % self.object.name

    def get_content_title(self):
        return mark_safe(
            escape(_("New ticket for %s"))
            % format_html(
                '<a href="{0}">{1}</a>',
                reverse("problem_detail", args=[self.object.code]),
                self.object.translated_name(self.request.LANGUAGE_CODE),
            )
        )

    def form_valid(self, form):
        if not self.object.is_accessible_by(self.request.user):
            raise Http404()
        return super().form_valid(form)


class TicketCommentForm(forms.Form):
    body = forms.CharField(widget=ticket_widget)


class TicketMixin(object):
    model = Ticket

    def get_object(self, queryset=None):
        ticket = super(TicketMixin, self).get_object(queryset)
        profile_id = self.request.profile.id
        if self.request.user.has_perm("judge.change_ticket"):
            return ticket
        if ticket.user_id == profile_id:
            return ticket
        if ticket.assignees.filter(id=profile_id).exists():
            return ticket
        linked = ticket.linked_item
        if isinstance(linked, Problem) and linked.is_editable_by(self.request.user):
            return ticket
        if isinstance(linked, BlogPost) and linked.is_editable_by(self.request.user):
            return ticket
        raise PermissionDenied()


class TicketView(TitleMixin, LoginRequiredMixin, TicketMixin, SingleObjectFormView):
    form_class = TicketCommentForm
    template_name = "ticket/ticket.html"
    context_object_name = "ticket"

    def form_valid(self, form):
        message = TicketMessage(
            user=self.request.profile,
            body=form.cleaned_data["body"],
            ticket=self.object,
        )
        message.save()

        link = "%s#message-%d" % (reverse("ticket", args=[self.object.id]), message.id)

        notify_list = list(chain(self.object.assignees.all(), [self.object.user]))
        add_ticket_notifications(notify_list, message.user, link, self.object)

        if event.real:
            event.post(
                "tickets",
                {
                    "type": "ticket-message",
                    "id": self.object.id,
                    "message": message.id,
                    "user": self.object.user_id,
                    "assignees": list(
                        self.object.assignees.values_list("id", flat=True)
                    ),
                },
            )
            event.post(
                "ticket-%d" % self.object.id,
                {
                    "type": "ticket-message",
                    "message": message.id,
                },
            )
        return HttpResponseRedirect(link)

    def get_title(self):
        return _("%(title)s - Ticket %(id)d") % {
            "title": self.object.title,
            "id": self.object.id,
        }

    def get_context_data(self, **kwargs):
        context = super(TicketView, self).get_context_data(**kwargs)
        context["ticket_messages"] = self.object.messages.all()
        context["assignees"] = self.object.assignees.all()
        context["last_msg"] = event.last()

        # Check if user can edit assignees
        can_edit_assignees = self.request.user.has_perm("judge.change_ticket")
        if not can_edit_assignees and self.object.linked_item:
            linked = self.object.linked_item
            if isinstance(linked, (BlogPost, Problem)):
                can_edit_assignees = linked.is_editable_by(self.request.user)

        context["can_edit_assignees"] = can_edit_assignees
        return context


class TicketStatusChangeView(LoginRequiredMixin, TicketMixin, SingleObjectMixin, View):
    open = None

    def post(self, request, *args, **kwargs):
        if self.open is None:
            raise ImproperlyConfigured("Need to define open")
        ticket = self.get_object()
        if ticket.is_open != self.open:
            with revisions.create_revision():
                ticket.is_open = self.open
                ticket.save()

                action = "Reopened" if self.open else "Closed"
                revisions.set_user(self.request.user)
                revisions.set_comment(f"{action} ticket")

            if event.real:
                event.post(
                    "tickets",
                    {
                        "type": "ticket-status",
                        "id": ticket.id,
                        "open": self.open,
                        "user": ticket.user_id,
                        "assignees": list(
                            ticket.assignees.values_list("id", flat=True)
                        ),
                        "title": ticket.title,
                    },
                )
                event.post(
                    "ticket-%d" % ticket.id,
                    {
                        "type": "ticket-status",
                        "open": self.open,
                    },
                )
        return HttpResponse(status=204)


class TicketNotesForm(forms.Form):
    notes = forms.CharField(widget=forms.Textarea(), required=False)


class TicketAssigneeForm(forms.Form):
    assignees = forms.ModelMultipleChoiceField(
        queryset=Profile.objects.all(),
        widget=HeavySelect2MultipleWidget(
            data_view="user_search_select2_ajax",
            attrs={
                "data-placeholder": _("Search for users to assign..."),
                "data-minimum-input-length": 1,
                "data-allow-clear": "true",
            },
        ),
        required=False,
        label=_("Assignees"),
    )


class TicketNotesEditView(LoginRequiredMixin, TicketMixin, SingleObjectFormView):
    template_name = "ticket/edit-notes.html"
    form_class = TicketNotesForm
    context_object_name = "ticket"

    def get_initial(self):
        return {"notes": self.get_object().notes}

    def form_valid(self, form):
        ticket = self.get_object()
        old_notes = ticket.notes
        new_notes = form.cleaned_data["notes"]

        with revisions.create_revision():
            ticket.notes = new_notes
            ticket.save()

            # Create descriptive comment about the change
            if old_notes != new_notes:
                if old_notes and new_notes:
                    comment = "Updated assignee notes"
                elif new_notes:
                    comment = "Added assignee notes"
                else:
                    comment = "Removed assignee notes"
            else:
                comment = "Notes updated (no changes)"

            revisions.set_user(self.request.user)
            revisions.set_comment(comment)

        if new_notes:
            return HttpResponse(linebreaks(new_notes, autoescape=True))
        else:
            return HttpResponse()

    def form_invalid(self, form):
        return HttpResponseBadRequest()


class TicketAssigneeEditView(LoginRequiredMixin, TicketMixin, SingleObjectFormView):
    template_name = "ticket/edit-assignees.html"
    form_class = TicketAssigneeForm
    context_object_name = "ticket"

    def get_initial(self):
        return {"assignees": self.get_object().assignees.all()}

    def form_valid(self, form):
        try:
            ticket = self.get_object()

            # Check if user can edit assignees (admins, or linked item editors)
            if not self.request.user.has_perm("judge.change_ticket"):
                linked = ticket.linked_item
                can_edit = False
                if isinstance(linked, BlogPost):
                    can_edit = linked.is_editable_by(self.request.user)
                elif isinstance(linked, Problem):
                    can_edit = linked.is_editable_by(self.request.user)

                if not can_edit:
                    return JsonResponse(
                        {
                            "success": False,
                            "error": _(
                                "You don't have permission to edit assignees for this ticket."
                            ),
                        },
                        status=403,
                    )

            old_assignees = set(ticket.assignees.all())
            new_assignees = set(form.cleaned_data["assignees"])

            with revisions.create_revision():
                ticket.assignees.set(form.cleaned_data["assignees"])

                # Create descriptive comment about the change
                if old_assignees != new_assignees:
                    added = new_assignees - old_assignees
                    removed = old_assignees - new_assignees

                    changes = []
                    if added:
                        changes.append(f"added {', '.join(a.username for a in added)}")
                    if removed:
                        changes.append(
                            f"removed {', '.join(r.username for r in removed)}"
                        )

                    comment = f"Updated assignees: {'; '.join(changes)}"
                else:
                    comment = "Assignees updated (no changes)"

                revisions.set_user(self.request.user)
                revisions.set_comment(comment)

            return JsonResponse({"success": True})
        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)}, status=500)

    def form_invalid(self, form):
        errors = []
        for field, field_errors in form.errors.items():
            for error in field_errors:
                errors.append(f"{field}: {error}")

        return JsonResponse(
            {
                "success": False,
                "error": "; ".join(errors) if errors else _("Invalid form data"),
            },
            status=400,
        )


class TicketList(LoginRequiredMixin, ListView):
    model = Ticket
    template_name = "ticket/list.html"
    context_object_name = "tickets"
    paginate_by = 50
    paginator_class = DiggPaginator

    @cached_property
    def user(self):
        return self.request.user

    @cached_property
    def profile(self):
        return self.user.profile

    @cached_property
    def can_edit_all(self):
        return self.request.user.has_perm("judge.change_ticket")

    @cached_property
    def filter_users(self):
        return self.request.GET.getlist("user")

    @cached_property
    def filter_assignees(self):
        return self.request.GET.getlist("assignee")

    def GET_with_session(self, key):
        if not self.request.GET:
            return self.request.session.get(key, False)
        return self.request.GET.get(key, None) == "1"

    def _get_queryset(self):
        return (
            Ticket.objects.select_related("user__user")
            .prefetch_related("assignees__user")
            .order_by("-id")
        )

    def get_queryset(self):
        queryset = self._get_queryset()
        if self.GET_with_session("own"):
            queryset = queryset.filter(own_ticket_filter(self.profile.id))
        elif not self.can_edit_all:
            queryset = filter_visible_tickets(queryset, self.user, self.profile)
        if self.filter_assignees:
            queryset = queryset.filter(
                assignees__user__username__in=self.filter_assignees
            )
        if self.filter_users:
            queryset = queryset.filter(user__user__username__in=self.filter_users)
        return queryset.distinct()

    def get_context_data(self, **kwargs):
        context = super(TicketList, self).get_context_data(**kwargs)

        page = context["page_obj"]
        context["title"] = _("Tickets - Page %(number)d of %(total)d") % {
            "number": page.number,
            "total": page.paginator.num_pages,
        }
        context["can_edit_all"] = self.can_edit_all
        context["filter_status"] = {
            "own": self.GET_with_session("own"),
            "user": self.filter_users,
            "assignee": self.filter_assignees,
            "user_id": json.dumps(
                list(
                    Profile.objects.filter(
                        user__username__in=self.filter_users
                    ).values_list("id", flat=True)
                )
            ),
            "assignee_id": json.dumps(
                list(
                    Profile.objects.filter(
                        user__username__in=self.filter_assignees
                    ).values_list("id", flat=True)
                )
            ),
            "own_id": self.profile.id if self.GET_with_session("own") else "null",
        }
        context["last_msg"] = event.last()
        context.update(paginate_query_context(self.request))
        return context

    def post(self, request, *args, **kwargs):
        to_update = ("own",)
        for key in to_update:
            if key in request.GET:
                val = request.GET.get(key) == "1"
                request.session[key] = val
            else:
                request.session.pop(key, None)
        return HttpResponseRedirect(request.get_full_path())


class ProblemTicketListView(TicketList):
    def _get_queryset(self):
        problem = get_object_or_404(Problem, code=self.kwargs.get("problem"))
        if problem.is_editable_by(self.request.user):
            return problem.tickets.order_by("-id")
        elif problem.is_accessible_by(self.request.user):
            return problem.tickets.filter(own_ticket_filter(self.profile.id)).order_by(
                "-id"
            )
        raise Http404()


class TicketListDataAjax(TicketMixin, SingleObjectMixin, View):
    def get(self, request, *args, **kwargs):
        try:
            self.kwargs["pk"] = request.GET["id"]
        except KeyError:
            return HttpResponseBadRequest()
        ticket = self.get_object()
        message = ticket.messages.first()
        return JsonResponse(
            {
                "row": get_template("ticket/row.html").render(
                    {"ticket": ticket}, request
                ),
                "notification": {
                    "title": _("New Ticket: %s") % ticket.title,
                    "body": "%s\n%s"
                    % (
                        _("#%(id)d, assigned to: %(users)s")
                        % {
                            "id": ticket.id,
                            "users": (
                                _(", ").join(
                                    ticket.assignees.values_list(
                                        "user__username", flat=True
                                    )
                                )
                                or _("no one")
                            ),
                        },
                        truncatechars(message.body, 200),
                    ),
                },
            }
        )


class TicketMessageDataAjax(TicketMixin, SingleObjectMixin, View):
    def get(self, request, *args, **kwargs):
        try:
            message_id = request.GET["message"]
        except KeyError:
            return HttpResponseBadRequest()
        ticket = self.get_object()
        try:
            message = ticket.messages.get(id=message_id)
        except TicketMessage.DoesNotExist:
            return HttpResponseBadRequest()
        return JsonResponse(
            {
                "message": get_template("ticket/message.html").render(
                    {"message": message}, request
                ),
                "notification": {
                    "title": _("New Ticket Message For: %s") % ticket.title,
                    "body": truncatechars(message.body, 200),
                },
            }
        )


class BlogMixin:
    """Mixin for blog post related views"""

    model = BlogPost
    pk_url_kwarg = "id"
    context_object_name = "post"

    def get_object(self, queryset=None):
        post = super().get_object(queryset)
        if not post.is_accessible_by(self.request.user):
            raise Http404()
        return post


class NewBlogTicketView(BlogMixin, TitleMixin, NewTicketView):
    template_name = "ticket/new_post.html"

    def get_assignees(self):
        return self.object.authors.all()

    def get_title(self):
        return _("New ticket for %s") % self.object.title

    def get_content_title(self):
        return mark_safe(
            escape(_("New ticket for %s"))
            % format_html(
                '<a href="{0}">{1}</a>',
                reverse("blog_post", args=[self.object.id, self.object.slug]),
                self.object.title,
            )
        )

    def form_valid(self, form):
        if not self.object.is_accessible_by(self.request.user):
            raise Http404()
        return super().form_valid(form)


class BlogTicketListView(TicketList):
    template_name = "ticket/list.html"

    def _get_queryset(self):
        post = get_object_or_404(BlogPost, id=self.kwargs.get("id"))
        if not post.is_accessible_by(self.request.user):
            raise Http404()
        if post.is_editable_by(self.request.user):
            return post.tickets.order_by("-id")
        else:
            return post.tickets.filter(own_ticket_filter(self.profile.id)).order_by(
                "-id"
            )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        post = get_object_or_404(BlogPost, id=self.kwargs.get("id"))
        context["post"] = post
        context["title"] = _("Tickets for %s") % post.title
        return context


class TicketLogView(LoginRequiredMixin, TicketMixin, TitleMixin, ListView):
    """View to display ticket history using revisions"""

    template_name = "ticket/logs.html"
    context_object_name = "logs"
    paginate_by = 50

    def get_queryset(self):
        ticket = self.get_object()
        # Get all versions of this ticket, ordered by revision date (newest first)
        versions = (
            Version.objects.get_for_object(ticket)
            .select_related("revision__user")
            .order_by("-revision__date_created")
        )

        # Process each version to add readable field information
        processed_versions = []
        for version in versions:
            # Get the actual object data from the version
            try:
                version_data = version.field_dict
                # Handle assignees field specially
                if "assignees" in version_data:
                    assignee_ids = version_data["assignees"]
                    if assignee_ids:
                        assignee_usernames = []
                        for assignee_id in assignee_ids:
                            try:
                                profile = Profile.objects.get(id=assignee_id)
                                assignee_usernames.append(profile.username)
                            except Profile.DoesNotExist:
                                assignee_usernames.append(
                                    f"Unknown User #{assignee_id}"
                                )
                        version_data["assignees_display"] = ", ".join(
                            assignee_usernames
                        )
                    else:
                        version_data["assignees_display"] = _("None")

                version.processed_fields = version_data
            except Exception:
                version.processed_fields = {}

            processed_versions.append(version)

        return processed_versions

    def get_object(self):
        # Get ticket object using TicketMixin
        return get_object_or_404(Ticket, pk=self.kwargs["pk"])

    def get_title(self):
        ticket = self.get_object()
        return _("History for Ticket #%(id)d: %(title)s") % {
            "id": ticket.id,
            "title": ticket.title,
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["ticket"] = self.get_object()
        return context


class TicketFeed(HomeFeedView):
    model = Ticket
    context_object_name = "tickets"
    paginate_by = 50
    feed_content_template_name = "ticket/feed.html"

    def get_queryset(self):
        profile = self.request.profile
        is_own = self.request.GET.get("view", "own") == "own"
        status_filter = self.request.GET.get("status", "all")  # all, open, closed

        if is_own:
            if self.request.user.is_authenticated:
                queryset = Ticket.objects.filter(
                    Q(user=profile) | Q(assignees__in=[profile])
                )
            else:
                queryset = Ticket.objects.none()
        else:
            # Superusers better be staffs, not the spell-casting kind either.
            if self.request.user.is_staff:
                queryset = Ticket.objects.all()
                queryset = filter_visible_tickets(queryset, self.request.user, profile)
            else:
                queryset = Ticket.objects.none()

        # Apply status filter
        if status_filter == "open":
            queryset = queryset.filter(is_open=True)
        elif status_filter == "closed":
            queryset = queryset.filter(is_open=False)

        return (
            queryset.annotate(
                last_message_id=Max("message__id"),
                last_action_time=Max("message__time"),
            )
            .order_by("-last_message_id")
            .prefetch_related("linked_item")
        )

    def get_context_data(self, **kwargs):
        context = super(TicketFeed, self).get_context_data(**kwargs)
        context["page_type"] = "ticket"
        context["view_type"] = self.request.GET.get("view", "own")
        context["status_filter"] = self.request.GET.get("status", "all")
        context["can_view_all"] = self.request.user.is_staff
        context["title"] = _("Ticket feed")

        return context
