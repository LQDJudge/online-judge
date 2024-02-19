from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q, Value, BooleanField
from django.db.utils import ProgrammingError
from django.forms import Form, modelformset_factory
from django.http import (
    Http404,
    HttpResponsePermanentRedirect,
    HttpResponseRedirect,
    HttpResponseBadRequest,
)
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.functional import cached_property
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _, gettext_lazy, ungettext
from django.views.generic import (
    DetailView,
    FormView,
    ListView,
    UpdateView,
    View,
    CreateView,
)
from django.views.generic.detail import (
    SingleObjectMixin,
    SingleObjectTemplateResponseMixin,
)
from django.core.paginator import Paginator
from django.contrib.sites.shortcuts import get_current_site
from reversion import revisions

from judge.forms import (
    EditOrganizationForm,
    AddOrganizationForm,
    AddOrganizationMemberForm,
    OrganizationBlogForm,
    OrganizationAdminBlogForm,
    EditOrganizationContestForm,
    ContestProblemFormSet,
    AddOrganizationContestForm,
)
from judge.models import (
    BlogPost,
    Comment,
    Organization,
    OrganizationRequest,
    Problem,
    Profile,
    Contest,
    ContestProblem,
    OrganizationProfile,
)
from judge.models.notification import make_notification
from judge import event_poster as event
from judge.utils.ranker import ranker
from judge.utils.views import (
    TitleMixin,
    generic_message,
    QueryStringSortMixin,
    DiggPaginatorMixin,
)
from judge.utils.problems import user_attempted_ids, user_completed_ids
from judge.views.problem import ProblemList
from judge.views.contests import ContestList
from judge.views.submission import AllSubmissions, SubmissionsListBase
from judge.views.feed import FeedView
from judge.tasks import rescore_contest

__all__ = [
    "OrganizationList",
    "OrganizationHome",
    "OrganizationUsers",
    "OrganizationProblems",
    "OrganizationContests",
    "OrganizationMembershipChange",
    "JoinOrganization",
    "LeaveOrganization",
    "EditOrganization",
    "RequestJoinOrganization",
    "OrganizationRequestDetail",
    "OrganizationRequestView",
    "OrganizationRequestLog",
    "KickUserWidgetView",
]


class OrganizationBase(object):
    def can_edit_organization(self, org=None):
        if org is None:
            org = self.object
        if self.request.profile:
            return self.request.profile.can_edit_organization(org)
        return False

    def is_member(self, org=None):
        if org is None:
            org = self.object
        return (
            self.request.profile in org if self.request.user.is_authenticated else False
        )

    def is_admin(self, org=None):
        if org is None:
            org = self.object
        if self.request.profile:
            return org.admins.filter(id=self.request.profile.id).exists()
        return False

    def can_access(self, org):
        if self.request.user.is_superuser:
            return True
        if org is None:
            org = self.object
        return self.is_member(org) or self.can_edit_organization(org)


class OrganizationMixin(OrganizationBase):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_member"] = self.is_member(self.organization)
        context["is_admin"] = self.is_admin(self.organization)
        context["can_edit"] = self.can_edit_organization(self.organization)
        context["organization"] = self.organization
        context["logo_override_image"] = self.organization.logo_override_image
        context["organization_subdomain"] = (
            ("http" if settings.DMOJ_SSL == 0 else "https")
            + "://"
            + self.organization.slug
            + "."
            + get_current_site(self.request).domain
        )
        if "organizations" in context:
            context.pop("organizations")
        return context

    def dispatch(self, request, *args, **kwargs):
        try:
            self.organization_id = int(kwargs["pk"])
            self.organization = get_object_or_404(Organization, id=self.organization_id)
        except Http404:
            key = None
            if hasattr(self, "slug_url_kwarg"):
                key = kwargs.get(self.slug_url_kwarg, None)
            if key:
                return generic_message(
                    request,
                    _("No such organization"),
                    _('Could not find an organization with the key "%s".') % key,
                    status=403,
                )
            else:
                return generic_message(
                    request,
                    _("No such organization"),
                    _("Could not find such organization."),
                    status=403,
                )
        if self.organization.slug != kwargs["slug"]:
            return HttpResponsePermanentRedirect(
                request.get_full_path().replace(kwargs["slug"], self.organization.slug)
            )
        if self.request.user.is_authenticated:
            OrganizationProfile.add_organization(
                self.request.profile, self.organization
            )

        return super(OrganizationMixin, self).dispatch(request, *args, **kwargs)


class AdminOrganizationMixin(OrganizationMixin):
    def dispatch(self, request, *args, **kwargs):
        res = super(AdminOrganizationMixin, self).dispatch(request, *args, **kwargs)
        if not hasattr(self, "organization") or self.can_edit_organization(
            self.organization
        ):
            return res
        return generic_message(
            request,
            _("Can't edit organization"),
            _("You are not allowed to edit this organization."),
            status=403,
        )


class MemberOrganizationMixin(OrganizationMixin):
    def dispatch(self, request, *args, **kwargs):
        res = super(MemberOrganizationMixin, self).dispatch(request, *args, **kwargs)
        if not hasattr(self, "organization") or self.can_access(self.organization):
            return res
        return generic_message(
            request,
            _("Can't access organization"),
            _("You are not allowed to access this organization."),
            status=403,
        )


class OrganizationHomeView(OrganizationMixin):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not hasattr(self, "organization"):
            self.organization = self.object
        if self.can_edit_organization(self.organization):
            context["pending_count"] = OrganizationRequest.objects.filter(
                state="P", organization=self.organization
            ).count()
            context["pending_blog_count"] = BlogPost.objects.filter(
                visible=False, organizations=self.organization
            ).count()
        else:
            context["pending_blog_count"] = BlogPost.objects.filter(
                visible=False,
                organizations=self.organization,
                authors=self.request.profile,
            ).count()
        context["top_rated"] = self.organization.members.filter(
            is_unlisted=False
        ).order_by("-rating")[:10]
        context["top_scorer"] = self.organization.members.filter(
            is_unlisted=False
        ).order_by("-performance_points")[:10]
        return context


class OrganizationList(TitleMixin, ListView, OrganizationBase):
    model = Organization
    context_object_name = "organizations"
    template_name = "organization/list.html"
    title = gettext_lazy("Groups")

    def get_queryset(self):
        return (
            super(OrganizationList, self)
            .get_queryset()
            .annotate(member_count=Count("member"))
        )

    def get_context_data(self, **kwargs):
        context = super(OrganizationList, self).get_context_data(**kwargs)
        context["my_organizations"] = []
        context["page_type"] = "organizations"
        if self.request.profile:
            context["my_organizations"] = context["organizations"].filter(
                id__in=self.request.profile.organizations.values("id")
            )
        other_organizations = context["organizations"].exclude(
            id__in=context["my_organizations"]
        )
        context["open_organizations"] = other_organizations.filter(is_open=True)
        context["private_organizations"] = other_organizations.filter(is_open=False)
        return context


class OrganizationHome(OrganizationHomeView, FeedView):
    template_name = "organization/home.html"
    paginate_by = 4
    context_object_name = "posts"
    feed_content_template_name = "blog/content.html"

    def get_queryset(self):
        return (
            BlogPost.objects.filter(
                visible=True,
                publish_on__lte=timezone.now(),
                is_organization_private=True,
                organizations=self.organization,
            )
            .order_by("-sticky", "-publish_on")
            .prefetch_related("authors__user", "organizations")
        )

    def get_context_data(self, **kwargs):
        context = super(OrganizationHome, self).get_context_data(**kwargs)
        context["title"] = self.organization.name

        now = timezone.now()
        visible_contests = (
            Contest.get_visible_contests(self.request.user)
            .filter(
                is_visible=True,
                is_organization_private=True,
                organizations=self.organization,
            )
            .order_by("start_time")
        )
        context["current_contests"] = visible_contests.filter(
            start_time__lte=now, end_time__gt=now
        )
        context["future_contests"] = visible_contests.filter(start_time__gt=now)
        context["page_type"] = "home"

        return context


class OrganizationUsers(
    DiggPaginatorMixin, QueryStringSortMixin, OrganizationMixin, ListView
):
    template_name = "organization/users.html"
    all_sorts = frozenset(("points", "problem_count", "rating", "performance_points"))
    default_desc = all_sorts
    default_sort = "-performance_points"
    paginate_by = 100
    context_object_name = "users"

    def get_queryset(self):
        return (
            self.organization.members.filter(is_unlisted=False)
            .order_by(self.order, "id")
            .select_related("user")
            .only(
                "display_rank",
                "user__username",
                "points",
                "rating",
                "performance_points",
                "problem_count",
            )
        )

    def dispatch(self, request, *args, **kwargs):
        res = super(OrganizationUsers, self).dispatch(request, *args, **kwargs)
        if res.status_code != 200:
            return res
        if self.can_access(self.organization) or self.organization.is_open:
            return res
        return generic_message(
            request,
            _("Can't access organization"),
            _("You are not allowed to access this organization."),
            status=403,
        )

    def get_context_data(self, **kwargs):
        context = super(OrganizationUsers, self).get_context_data(**kwargs)
        context["title"] = _("%s Members") % self.organization.name
        context["partial"] = True
        context["kick_url"] = reverse(
            "organization_user_kick",
            args=[self.organization.id, self.organization.slug],
        )
        context["users"] = ranker(
            context["users"], rank=self.paginate_by * (context["page_obj"].number - 1)
        )

        context["first_page_href"] = "."
        context["page_type"] = "users"
        context.update(self.get_sort_context())
        return context


class OrganizationProblems(LoginRequiredMixin, MemberOrganizationMixin, ProblemList):
    template_name = "organization/problems.html"
    filter_organization = True

    def get_queryset(self):
        self.org_query = [self.organization_id]
        return super().get_normal_queryset()

    def get(self, request, *args, **kwargs):
        self.setup_problem_list(request)
        return super().get(request, *args, **kwargs)

    def get_completed_problems(self):
        return user_completed_ids(self.profile) if self.profile is not None else ()

    def get_attempted_problems(self):
        return user_attempted_ids(self.profile) if self.profile is not None else ()

    @cached_property
    def in_contest(self):
        return False

    def get_context_data(self, **kwargs):
        context = super(OrganizationProblems, self).get_context_data(**kwargs)
        context["page_type"] = "problems"
        context["show_contest_mode"] = False
        return context


class OrganizationContestMixin(
    LoginRequiredMixin,
    TitleMixin,
    OrganizationHomeView,
):
    model = Contest

    def is_contest_editable(self, request, contest):
        return contest.is_editable_by(request.user) or self.can_edit_organization(
            self.organization
        )


class OrganizationContests(
    OrganizationContestMixin, MemberOrganizationMixin, ContestList
):
    template_name = "organization/contests.html"

    def get_queryset(self):
        self.org_query = [self.organization_id]
        return super().get_queryset()

    def set_editable_contest(self, contest):
        if not contest:
            return False
        contest.is_editable = self.is_contest_editable(self.request, contest)

    def get_context_data(self, **kwargs):
        context = super(OrganizationContests, self).get_context_data(**kwargs)
        context["page_type"] = "contests"
        context["hide_contest_orgs"] = True
        context.pop("organizations")
        context["create_url"] = reverse(
            "organization_contest_add",
            args=[self.organization.id, self.organization.slug],
        )

        for participation in context["active_participations"]:
            self.set_editable_contest(participation.contest)
        for contest in context["past_contests"]:
            self.set_editable_contest(contest)
        for contest in context["current_contests"]:
            self.set_editable_contest(contest)
        for contest in context["future_contests"]:
            self.set_editable_contest(contest)
        return context


class OrganizationSubmissions(
    LoginRequiredMixin, MemberOrganizationMixin, SubmissionsListBase
):
    template_name = "organization/submissions.html"

    @cached_property
    def in_contest(self):
        return False

    @cached_property
    def contest(self):
        return None

    def get_context_data(self, **kwargs):
        context = super(OrganizationSubmissions, self).get_context_data(**kwargs)
        # context["dynamic_update"] = context["page_obj"].number == 1
        # context["last_msg"] = event.last()
        context["stats_update_interval"] = 3600
        context["page_type"] = "submissions"
        context["page_prefix"] = None
        context["page_suffix"] = suffix = (
            ("?" + self.request.GET.urlencode()) if self.request.GET else ""
        )
        context["first_page_href"] = (self.first_page_href or ".") + suffix

        return context

    def get_content_title(self):
        return format_html(
            _('All submissions in <a href="{1}">{0}</a>'),
            self.organization,
            reverse(
                "organization_home", args=[self.organization.id, self.organization.slug]
            ),
        )


class OrganizationMembershipChange(
    LoginRequiredMixin, OrganizationMixin, SingleObjectMixin, View
):
    model = Organization
    context_object_name = "organization"

    def post(self, request, *args, **kwargs):
        org = self.get_object()
        response = self.handle(request, org, request.profile)
        if response is not None:
            return response
        return HttpResponseRedirect(org.get_absolute_url())

    def handle(self, request, org, profile):
        raise NotImplementedError()


class JoinOrganization(OrganizationMembershipChange):
    def handle(self, request, org, profile):
        if profile.organizations.filter(id=org.id).exists():
            return generic_message(
                request,
                _("Joining group"),
                _("You are already in the group."),
            )

        if not org.is_open:
            return generic_message(
                request, _("Joining group"), _("This group is not open.")
            )

        max_orgs = settings.DMOJ_USER_MAX_ORGANIZATION_COUNT
        if profile.organizations.filter(is_open=True).count() >= max_orgs:
            return generic_message(
                request,
                _("Joining group"),
                _("You may not be part of more than {count} public groups.").format(
                    count=max_orgs
                ),
            )

        profile.organizations.add(org)
        profile.save()
        cache.delete(make_template_fragment_key("org_member_count", (org.id,)))


class LeaveOrganization(OrganizationMembershipChange):
    def handle(self, request, org, profile):
        if not profile.organizations.filter(id=org.id).exists():
            return generic_message(
                request,
                _("Leaving group"),
                _('You are not in "%s".') % org.short_name,
            )
        profile.organizations.remove(org)
        cache.delete(make_template_fragment_key("org_member_count", (org.id,)))


class OrganizationRequestForm(Form):
    reason = forms.CharField(widget=forms.Textarea)


class RequestJoinOrganization(LoginRequiredMixin, SingleObjectMixin, FormView):
    model = Organization
    slug_field = "key"
    slug_url_kwarg = "key"
    template_name = "organization/requests/request.html"
    form_class = OrganizationRequestForm

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()
        return super(RequestJoinOrganization, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(RequestJoinOrganization, self).get_context_data(**kwargs)
        if self.object.is_open:
            raise Http404()
        context["title"] = _("Request to join %s") % self.object.name
        return context

    def form_valid(self, form):
        request = OrganizationRequest()
        request.organization = self.get_object()
        request.user = self.request.profile
        request.reason = form.cleaned_data["reason"]
        request.state = "P"
        request.save()
        return HttpResponseRedirect(
            reverse(
                "request_organization_detail",
                args=(
                    request.organization.id,
                    request.organization.slug,
                    request.id,
                ),
            )
        )


class OrganizationRequestDetail(
    LoginRequiredMixin,
    TitleMixin,
    OrganizationHomeView,
    DetailView,
):
    model = OrganizationRequest
    template_name = "organization/requests/detail.html"
    title = gettext_lazy("Join request detail")
    pk_url_kwarg = "rpk"

    def get_object(self, queryset=None):
        object = super(OrganizationRequestDetail, self).get_object(queryset)
        profile = self.request.profile
        if (
            object.user_id != profile.id
            and not object.organization.admins.filter(id=profile.id).exists()
        ):
            raise PermissionDenied()
        return object


OrganizationRequestFormSet = modelformset_factory(
    OrganizationRequest, extra=0, fields=("state",), can_delete=True
)


class OrganizationRequestBaseView(
    DetailView,
    OrganizationHomeView,
    TitleMixin,
    LoginRequiredMixin,
    SingleObjectTemplateResponseMixin,
    SingleObjectMixin,
):
    model = Organization
    slug_field = "key"
    slug_url_kwarg = "key"
    tab = None

    def get_object(self, queryset=None):
        organization = super(OrganizationRequestBaseView, self).get_object(queryset)
        if not (
            organization.admins.filter(id=self.request.profile.id).exists()
            or organization.registrant_id == self.request.profile.id
        ):
            raise PermissionDenied()
        return organization

    def get_content_title(self):
        return _("Manage join requests")

    def get_context_data(self, **kwargs):
        context = super(OrganizationRequestBaseView, self).get_context_data(**kwargs)
        context["title"] = _("Managing join requests for %s") % self.object.name
        context["tab"] = self.tab
        return context


class OrganizationRequestView(OrganizationRequestBaseView):
    template_name = "organization/requests/pending.html"
    tab = "pending"

    def get_context_data(self, **kwargs):
        context = super(OrganizationRequestView, self).get_context_data(**kwargs)
        context["formset"] = self.formset
        return context

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        self.formset = OrganizationRequestFormSet(
            queryset=OrganizationRequest.objects.filter(
                state="P", organization=self.object
            ),
        )
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def post(self, request, *args, **kwargs):
        self.object = organization = self.get_object()
        self.formset = formset = OrganizationRequestFormSet(request.POST, request.FILES)
        if formset.is_valid():
            if organization.slots is not None:
                deleted_set = set(formset.deleted_forms)
                to_approve = sum(
                    form.cleaned_data["state"] == "A"
                    for form in formset.forms
                    if form not in deleted_set
                )
                can_add = organization.slots - organization.members.count()
                if to_approve > can_add:
                    messages.error(
                        request,
                        _(
                            "Your organization can only receive %d more members. "
                            "You cannot approve %d users."
                        )
                        % (can_add, to_approve),
                    )
                    return self.render_to_response(
                        self.get_context_data(object=organization)
                    )

            approved, rejected = 0, 0
            for obj in formset.save():
                if obj.state == "A":
                    obj.user.organizations.add(obj.organization)
                    approved += 1
                elif obj.state == "R":
                    rejected += 1
            messages.success(
                request,
                ungettext("Approved %d user.", "Approved %d users.", approved)
                % approved
                + "\n"
                + ungettext("Rejected %d user.", "Rejected %d users.", rejected)
                % rejected,
            )
            cache.delete(
                make_template_fragment_key("org_member_count", (organization.id,))
            )
            return HttpResponseRedirect(request.get_full_path())
        return self.render_to_response(self.get_context_data(object=organization))

    put = post


class OrganizationRequestLog(OrganizationRequestBaseView):
    states = ("A", "R")
    tab = "log"
    template_name = "organization/requests/log.html"

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super(OrganizationRequestLog, self).get_context_data(**kwargs)
        context["requests"] = self.object.requests.filter(state__in=self.states)
        return context


class AddOrganizationMember(
    LoginRequiredMixin,
    TitleMixin,
    AdminOrganizationMixin,
    OrganizationHomeView,
    UpdateView,
):
    template_name = "organization/add-member.html"
    model = Organization
    form_class = AddOrganizationMemberForm

    def get_title(self):
        return _("Add member for %s") % self.object.name

    def get_object(self, queryset=None):
        object = super(AddOrganizationMember, self).get_object()
        if not self.can_edit_organization(object):
            raise PermissionDenied()
        return object

    def form_valid(self, form):
        new_users = form.cleaned_data["new_users"]
        self.object.members.add(*new_users)
        with revisions.create_revision():
            revisions.set_comment(_("Added members from site"))
            revisions.set_user(self.request.user)
            return super(AddOrganizationMember, self).form_valid(form)

    def get_success_url(self):
        return reverse("organization_users", args=[self.object.id, self.object.slug])


class KickUserWidgetView(
    LoginRequiredMixin, AdminOrganizationMixin, SingleObjectMixin, View
):
    model = Organization

    def post(self, request, *args, **kwargs):
        organization = self.get_object()
        try:
            user = Profile.objects.get(id=request.POST.get("user", None))
        except Profile.DoesNotExist:
            return generic_message(
                request,
                _("Can't kick user"),
                _("The user you are trying to kick does not exist!"),
                status=400,
            )

        if not organization.members.filter(id=user.id).exists():
            return generic_message(
                request,
                _("Can't kick user"),
                _("The user you are trying to kick is not in organization: %s.")
                % organization.name,
                status=400,
            )

        organization.members.remove(user)
        return HttpResponseRedirect(organization.get_users_url())


class EditOrganization(
    LoginRequiredMixin,
    TitleMixin,
    AdminOrganizationMixin,
    OrganizationHomeView,
    UpdateView,
):
    template_name = "organization/edit.html"
    model = Organization
    form_class = EditOrganizationForm

    def get_title(self):
        return _("Edit %s") % self.object.name

    def get_object(self, queryset=None):
        object = super(EditOrganization, self).get_object()
        if not self.can_edit_organization(object):
            raise PermissionDenied()
        return object

    def get_form(self, form_class=None):
        form = super(EditOrganization, self).get_form(form_class)
        form.fields["admins"].queryset = Profile.objects.filter(
            Q(organizations=self.object) | Q(admin_of=self.object)
        ).distinct()
        return form

    def form_valid(self, form):
        with revisions.create_revision():
            revisions.set_comment(_("Edited from site"))
            revisions.set_user(self.request.user)
            return super(EditOrganization, self).form_valid(form)


class AddOrganization(LoginRequiredMixin, TitleMixin, CreateView):
    template_name = "organization/add.html"
    model = Organization
    form_class = AddOrganizationForm

    def get_title(self):
        return _("Create group")

    def get_form_kwargs(self):
        kwargs = super(AddOrganization, self).get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def form_valid(self, form):
        if (
            not self.request.user.is_staff
            and Organization.objects.filter(registrant=self.request.profile).count()
            >= settings.DMOJ_USER_MAX_ORGANIZATION_ADD
        ):
            return generic_message(
                self.request,
                _("Exceeded limit"),
                _("You created too many groups. You can only create at most %d groups")
                % settings.DMOJ_USER_MAX_ORGANIZATION_ADD,
                status=400,
            )
        with revisions.create_revision():
            revisions.set_comment(_("Added from site"))
            revisions.set_user(self.request.user)
            res = super(AddOrganization, self).form_valid(form)
            self.object.admins.add(self.request.profile)
            self.object.members.add(self.request.profile)
            self.object.save()
            return res


class AddOrganizationContest(
    AdminOrganizationMixin, OrganizationContestMixin, CreateView
):
    template_name = "organization/contest/add.html"
    form_class = AddOrganizationContestForm

    def get_title(self):
        return _("Add contest")

    def get_form_kwargs(self):
        kwargs = super(AddOrganizationContest, self).get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def form_valid(self, form):
        with revisions.create_revision():
            revisions.set_comment(_("Added from site"))
            revisions.set_user(self.request.user)

            res = super(AddOrganizationContest, self).form_valid(form)

            self.object.organizations.add(self.organization)
            self.object.is_organization_private = True
            self.object.authors.add(self.request.profile)
            self.object.save()
            return res

    def get_success_url(self):
        return reverse(
            "organization_contest_edit",
            args=[self.organization.id, self.organization.slug, self.object.key],
        )


class EditOrganizationContest(
    OrganizationContestMixin, MemberOrganizationMixin, UpdateView
):
    template_name = "organization/contest/edit.html"
    form_class = EditOrganizationContestForm

    def setup_contest(self, request, *args, **kwargs):
        contest_key = kwargs.get("contest", None)
        if not contest_key:
            raise Http404()
        self.contest = get_object_or_404(Contest, key=contest_key)
        if self.organization not in self.contest.organizations.all():
            raise Http404()
        if not self.is_contest_editable(request, self.contest):
            return generic_message(
                self.request,
                _("Permission denied"),
                _("You are not allowed to edit this contest"),
                status=400,
            )

    def get_form_kwargs(self):
        kwargs = super(EditOrganizationContest, self).get_form_kwargs()
        kwargs["org_id"] = self.organization.id
        return kwargs

    def get(self, request, *args, **kwargs):
        res = self.setup_contest(request, *args, **kwargs)
        if res:
            return res
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        res = self.setup_contest(request, *args, **kwargs)
        if res:
            return res
        problem_formset = self.get_problem_formset(True)
        if problem_formset.is_valid():
            for problem_form in problem_formset.save(commit=False):
                if problem_form:
                    problem_form.contest = self.contest
                    problem_form.save()
            for problem_form in problem_formset.deleted_objects:
                problem_form.delete()
            super().post(request, *args, **kwargs)
            return HttpResponseRedirect(
                reverse(
                    "organization_contest_edit",
                    args=(
                        self.organization_id,
                        self.organization.slug,
                        self.contest.key,
                    ),
                )
            )

        self.object = self.contest
        return self.render_to_response(
            self.get_context_data(
                problems_form=problem_formset,
            )
        )

    def get_title(self):
        return _("Edit %s") % self.contest.key

    def get_content_title(self):
        href = reverse("contest_view", args=[self.contest.key])
        return mark_safe(_("Edit") + f' <a href="{href}">{self.contest.key}</a>')

    def get_object(self):
        return self.contest

    def form_valid(self, form):
        with revisions.create_revision():
            revisions.set_comment(_("Edited from site"))
            revisions.set_user(self.request.user)
            res = super(EditOrganizationContest, self).form_valid(form)
            self.object.organizations.add(self.organization)
            self.object.is_organization_private = True
            self.object.save()

            if any(
                f in form.changed_data
                for f in (
                    "start_time",
                    "end_time",
                    "time_limit",
                    "format_config",
                    "format_name",
                    "freeze_after",
                )
            ):
                transaction.on_commit(rescore_contest.s(self.object.key).delay)
            return res

    def get_problem_formset(self, post=False):
        return ContestProblemFormSet(
            data=self.request.POST if post else None,
            prefix="problems",
            queryset=ContestProblem.objects.filter(contest=self.contest).order_by(
                "order"
            ),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if "problems_form" not in context:
            context["problems_form"] = self.get_problem_formset()
        return context

    def get_success_url(self):
        return self.request.path


class AddOrganizationBlog(
    LoginRequiredMixin,
    TitleMixin,
    OrganizationHomeView,
    MemberOrganizationMixin,
    CreateView,
):
    template_name = "organization/blog/add.html"
    model = BlogPost
    form_class = OrganizationBlogForm

    def get_form_class(self):
        if self.can_edit_organization(self.organization):
            return OrganizationAdminBlogForm
        return OrganizationBlogForm

    def get_title(self):
        return _("Add blog for %s") % self.organization.name

    def form_valid(self, form):
        with revisions.create_revision():
            res = super(AddOrganizationBlog, self).form_valid(form)
            self.object.is_organization_private = True
            self.object.authors.add(self.request.profile)
            self.object.slug = self.organization.slug + "-" + self.request.user.username
            self.object.organizations.add(self.organization)
            self.object.save()

            revisions.set_comment(_("Added from site"))
            revisions.set_user(self.request.user)

            link = reverse(
                "edit_organization_blog",
                args=[self.organization.id, self.organization.slug, self.object.id],
            )
            html = (
                f'<a href="{link}">{self.object.title} - {self.organization.name}</a>'
            )
            make_notification(
                self.organization.admins.all(), "Add blog", html, self.request.profile
            )
            return res

    def get_success_url(self):
        return reverse(
            "organization_home", args=[self.organization.id, self.organization.slug]
        )


class EditOrganizationBlog(
    LoginRequiredMixin,
    TitleMixin,
    OrganizationHomeView,
    MemberOrganizationMixin,
    UpdateView,
):
    template_name = "organization/blog/edit.html"
    model = BlogPost

    def get_form_class(self):
        if self.can_edit_organization(self.organization):
            return OrganizationAdminBlogForm
        return OrganizationBlogForm

    def setup_blog(self, request, *args, **kwargs):
        try:
            self.blog_id = kwargs["blog_pk"]
            self.blog = BlogPost.objects.get(id=self.blog_id)
            if self.organization not in self.blog.organizations.all():
                raise Exception("This blog does not belong to this organization")
            if (
                self.request.profile not in self.blog.authors.all()
                and not self.can_edit_organization(self.organization)
            ):
                raise Exception("Not allowed to edit this blog")
        except:
            return generic_message(
                request,
                _("Permission denied"),
                _("Not allowed to edit this blog"),
            )

    def delete_blog(self, request, *args, **kwargs):
        self.blog_id = kwargs["blog_pk"]
        BlogPost.objects.get(pk=self.blog_id).delete()

    def get(self, request, *args, **kwargs):
        res = self.setup_blog(request, *args, **kwargs)
        if res:
            return res
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        res = self.setup_blog(request, *args, **kwargs)
        if res:
            return res
        if request.POST["action"] == "Delete":
            self.create_notification("Delete blog")
            self.delete_blog(request, *args, **kwargs)
            cur_url = reverse(
                "organization_pending_blogs",
                args=(self.organization_id, self.organization.slug),
            )
            return HttpResponseRedirect(cur_url)
        else:
            return super().post(request, *args, **kwargs)

    def get_object(self):
        return self.blog

    def get_title(self):
        return _("Edit blog %s") % self.object.title

    def create_notification(self, action):
        blog = BlogPost.objects.get(pk=self.blog_id)
        link = reverse(
            "edit_organization_blog",
            args=[self.organization.id, self.organization.slug, self.blog_id],
        )
        html = f'<a href="{link}">{blog.title} - {self.organization.name}</a>'
        post_authors = blog.authors.all()
        posible_users = self.organization.admins.all() | post_authors
        make_notification(posible_users, action, html, self.request.profile)

    def form_valid(self, form):
        with revisions.create_revision():
            res = super(EditOrganizationBlog, self).form_valid(form)
            revisions.set_comment(_("Edited from site"))
            revisions.set_user(self.request.user)
            self.create_notification("Edit blog")
            return res

    def get_success_url(self):
        return reverse(
            "organization_home", args=[self.organization.id, self.organization.slug]
        )


class PendingBlogs(
    LoginRequiredMixin,
    TitleMixin,
    MemberOrganizationMixin,
    OrganizationHomeView,
    ListView,
):
    model = BlogPost
    template_name = "organization/blog/pending.html"
    context_object_name = "blogs"

    def get_queryset(self):
        queryset = BlogPost.objects.filter(
            organizations=self.organization, visible=False
        )
        if not self.can_edit_organization(self.organization):
            queryset = queryset.filter(authors=self.request.profile)
        return queryset.order_by("publish_on")

    def get_title(self):
        return _("Pending blogs in %s") % self.organization.name
