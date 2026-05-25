import logging

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, JsonResponse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views.generic import TemplateView, View

from judge.ml.semantic_search import (
    SemanticSearchUnavailable,
    clamp_limit,
    search_problems,
    similar_problems,
)
from judge.models import Problem
from judge.utils.ratelimit import ratelimit

logger = logging.getLogger(__name__)


class SemanticSearch(LoginRequiredMixin, TemplateView):
    title = _("Semantic Search")
    template_name = "semantic_search.html"

    def get(self, request, *args, **kwargs):
        if not getattr(settings, "USE_ML", False):
            raise Http404()
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_type"] = "semantic_search"
        context["title"] = self.title
        context["default_limit"] = 50
        context["max_limit"] = 50
        return context


class SemanticSearchApiMixin(View):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": _("Authentication required")}, status=401)
        if not getattr(settings, "USE_ML", False):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)


@method_decorator(
    ratelimit(key="user", rate=settings.RL_SEMANTIC_SEARCH), name="dispatch"
)
class SemanticSearchApi(SemanticSearchApiMixin):
    def get(self, request, *args, **kwargs):
        query = request.GET.get("q", "").strip()
        if not query:
            return JsonResponse({"error": _("Query is required")}, status=400)

        limit = clamp_limit(request.GET.get("limit"))
        try:
            results = search_problems(query, limit=limit)
        except SemanticSearchUnavailable as exc:
            return JsonResponse({"error": str(exc)}, status=503)
        except Exception as exc:
            logger.error("Semantic search failed: %s", exc, exc_info=True)
            return JsonResponse({"error": str(exc)}, status=500)

        return JsonResponse({"query": query, "limit": limit, "results": results})


@method_decorator(
    ratelimit(key="user", rate=settings.RL_SEMANTIC_SEARCH), name="dispatch"
)
class SimilarProblemsApi(SemanticSearchApiMixin):
    def get(self, request, *args, **kwargs):
        problem_id = request.GET.get("problem_id", "").strip()
        problem_code = request.GET.get("problem", "").strip()
        if not problem_id and not problem_code:
            return JsonResponse({"error": _("Problem is required")}, status=400)

        try:
            if problem_id:
                problem = Problem.objects.get(id=problem_id)
            else:
                problem = Problem.objects.get(code=problem_code)
        except (Problem.DoesNotExist, ValueError):
            return JsonResponse({"error": _("Problem not found")}, status=404)

        if not problem.is_accessible_by(request.user):
            return JsonResponse({"error": _("Problem not found")}, status=404)

        limit = clamp_limit(request.GET.get("limit"))
        try:
            results = similar_problems(problem, limit=limit)
        except SemanticSearchUnavailable as exc:
            return JsonResponse({"error": str(exc)}, status=503)
        except Exception as exc:
            logger.error("Similar problem search failed: %s", exc, exc_info=True)
            return JsonResponse({"error": str(exc)}, status=500)

        return JsonResponse(
            {"problem": problem.code, "limit": limit, "results": results}
        )
