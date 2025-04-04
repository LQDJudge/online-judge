from urllib.parse import urljoin

from django.db.models import F, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.encoding import smart_str
from django.views.generic.list import BaseListView
from django.conf import settings

from chat_box.utils import encrypt_url

from judge.jinja2.gravatar import gravatar
from judge.models import Contest, Organization, Problem, Profile


def _get_user_queryset(term, org_id=None):
    if org_id:
        try:
            qs = Organization.objects.get(id=org_id).members.all()
        except Exception:
            raise Http404()
    else:
        qs = Profile.objects
    if term.endswith(" "):
        qs = qs.filter(user__username=term.strip())
    else:
        qs = qs.filter(user__username__icontains=term)
    return qs


class Select2View(BaseListView):
    paginate_by = 20

    def get(self, request, *args, **kwargs):
        self.request = request
        self.term = kwargs.get("term", request.GET.get("term", ""))
        self.object_list = self.get_queryset()
        context = self.get_context_data()

        return JsonResponse(
            {
                "results": [
                    {
                        "text": smart_str(self.get_name(obj)),
                        "id": obj.pk,
                    }
                    for obj in context["object_list"]
                ],
                "more": context["page_obj"].has_next(),
            }
        )

    def get_name(self, obj):
        return str(obj)


class UserSelect2View(Select2View):
    def get(self, request, *args, **kwargs):
        self.org_id = kwargs.get("org_id", request.GET.get("org_id", ""))
        return super(UserSelect2View, self).get(request, *args, **kwargs)

    def get_queryset(self):
        return (
            _get_user_queryset(self.term, self.org_id)
            .annotate(username=F("user__username"))
            .only("id")
        )

    def get_name(self, obj):
        return obj.username


class OrganizationSelect2View(Select2View):
    def get_queryset(self):
        return Organization.objects.filter(name__icontains=self.term)


class ProblemSelect2View(Select2View):
    def get_queryset(self):
        return (
            Problem.get_visible_problems(self.request.user)
            .filter(Q(code__icontains=self.term) | Q(name__icontains=self.term))
            .distinct()
        )


class ContestSelect2View(Select2View):
    def get(self, request, *args, **kwargs):
        self.problem_id = kwargs.get("problem_id", request.GET.get("problem_id", ""))
        return super(ContestSelect2View, self).get(request, *args, **kwargs)

    def get_queryset(self):
        q = Contest.get_visible_contests(self.request.user).filter(
            Q(key__icontains=self.term) | Q(name__icontains=self.term)
        )
        if self.problem_id:
            q = q.filter(problems=self.problem_id)
        return q


class UserSearchSelect2View(BaseListView):
    paginate_by = 20

    def get_queryset(self):
        return _get_user_queryset(self.term)

    def get_json_result_from_object(self, pk):
        profile = Profile(id=pk)
        return {
            "text": profile.username,
            "id": profile.username,
            "gravatar_url": gravatar(
                pk,
                self.gravatar_size,
            ),
            "display_rank": profile.get_display_rank(),
        }

    def get(self, request, *args, **kwargs):
        self.request = request
        self.kwargs = kwargs
        self.term = kwargs.get("term", request.GET.get("term", ""))
        self.gravatar_size = request.GET.get("gravatar_size", 32)

        self.object_list = self.get_queryset().values_list("pk", flat=True)

        context = self.get_context_data()

        return JsonResponse(
            {
                "results": [
                    self.get_json_result_from_object(pk)
                    for pk in context["object_list"]
                ],
                "more": context["page_obj"].has_next(),
            }
        )

    def get_name(self, obj):
        return str(obj)


class ContestUserSearchSelect2View(UserSearchSelect2View):
    def get_queryset(self):
        contest = get_object_or_404(Contest, key=self.kwargs["contest"])
        if not contest.is_accessible_by(
            self.request.user
        ) or not contest.can_see_full_scoreboard(self.request.user):
            raise Http404()

        return Profile.objects.filter(
            contest_history__contest=contest, user__username__icontains=self.term
        ).distinct()


class TicketUserSelect2View(UserSearchSelect2View):
    def get_queryset(self):
        return Profile.objects.filter(
            tickets__isnull=False, user__username__icontains=self.term
        ).distinct()


class AssigneeSelect2View(UserSearchSelect2View):
    def get_queryset(self):
        return Profile.objects.filter(
            assigned_tickets__isnull=False, user__username__icontains=self.term
        ).distinct()


class ChatUserSearchSelect2View(UserSearchSelect2View):
    def get_json_result_from_object(self, pk):
        if not self.request.user.is_authenticated:
            raise Http404()
        profile = Profile(id=pk)
        return {
            "text": profile.username,
            "id": encrypt_url(self.request.profile.id, pk),
            "gravatar_url": gravatar(
                pk,
                self.gravatar_size,
            ),
            "display_rank": profile.get_display_rank(),
        }


class ProblemAuthorSearchSelect2View(UserSearchSelect2View):
    def get_queryset(self):
        return Profile.objects.filter(
            authored_problems__isnull=False, user__username__icontains=self.term
        ).distinct()

    def get_json_result_from_object(self, pk):
        return {
            "text": Profile(pk).username,
            "id": pk,
        }
