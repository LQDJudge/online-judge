"""
Views for the problem author chatbot feature.
Provides AI-powered assistance for problem authors.
"""

import logging

from django.core.cache import cache
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.views.generic import View
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin

from judge.chatbot.cache import (
    clear_conversation,
    delete_message,
    get_conversation,
    set_model,
)
from judge.markdown import markdown as render_markdown
from judge.models import Problem
from judge.tasks.chatbot import STREAM_CACHE_PREFIX, chatbot_respond_task
from judge.views.problem import ProblemMixin, TitleMixin
from judge.utils.permissions import can_use_ai_features
from judge.utils.views import short_circuit_middleware
from llm_service.config import get_config

logger = logging.getLogger(__name__)


def _get_editable_problem(user, code):
    queryset = Problem.objects.filter(code=code)
    if not user.is_superuser:
        queryset = queryset.filter(Q(authors=user.profile) | Q(curators=user.profile))
    return queryset.distinct().get()


class ProblemChatbotView(
    ProblemMixin,
    TitleMixin,
    TemplateResponseMixin,
    SingleObjectMixin,
    View,
):
    """Main chatbot page for problem authors."""

    template_name = "problem/chatbot.html"

    def get_title(self):
        return _("AI Assistant - {0}").format(self.object.name)

    def get_content_title(self):
        return mark_safe(
            escape(_("AI Assistant for %s"))
            % (
                format_html(
                    '<a href="{1}">{0}</a>',
                    self.object.name,
                    reverse("problem_detail", args=[self.object.code]),
                )
            )
        )

    def has_permission(self):
        """Users with AI permission can access the chatbot for editable problems."""
        if not can_use_ai_features(self.request.user):
            return False
        self.object = self.get_object()
        return self.object.is_editable_by(self.request.user)

    def get(self, request, *args, **kwargs):
        if not self.has_permission():
            raise Http404()

        # Load conversation history from cache
        conversation = get_conversation(
            request.user.id,
            self.object.id,
            legacy_problem_code=self.object.code,
        )
        chat_messages = conversation.get("messages", [])
        current_model = conversation.get("model")

        # Get supported models
        config = get_config()
        supported_models = config.get_chatbot_supported_models()

        return self.render_to_response(
            {
                "problem": self.object,
                "chat_messages": chat_messages,
                "current_model": current_model,
                "supported_models": supported_models,
                "title": self.get_title(),
                "content_title": self.get_content_title(),
            }
        )


class ChatbotSendMessage(View):
    """API endpoint to send a message and get response via Celery task."""

    def post(self, request, problem):
        # Check authentication
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Not authenticated"}, status=401)
        if not can_use_ai_features(request.user):
            return JsonResponse({"error": "Permission denied"}, status=403)

        # Get problem
        try:
            problem_obj = _get_editable_problem(request.user, problem)
        except Problem.DoesNotExist:
            return JsonResponse({"error": "Problem not found"}, status=404)

        # Get message from request
        message = request.POST.get("message", "").strip()
        if not message:
            return JsonResponse({"error": "Message is required"}, status=400)

        # Limit message length
        if len(message) > 10000:
            return JsonResponse(
                {"error": "Message too long (max 10000 characters)"}, status=400
            )

        try:
            user_content_html = render_markdown(message)
        except Exception as md_error:
            logger.warning(f"User message markdown rendering failed: {md_error}")
            user_content_html = (
                '<div class="md-typeset content-description">'
                + escape(message).replace("\n", "<br>")
                + "</div>"
            )

        task = chatbot_respond_task.delay(
            user_id=request.user.id,
            problem_id=problem_obj.id,
            user_message=message,
        )

        return JsonResponse(
            {
                "success": True,
                "task_id": task.id,
                "user_content": user_content_html,
            }
        )


class ChatbotClearHistory(View):
    """API endpoint to clear conversation history."""

    def post(self, request, problem):
        # Check authentication
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Not authenticated"}, status=401)
        if not can_use_ai_features(request.user):
            return JsonResponse({"error": "Permission denied"}, status=403)

        # Get problem (validate it exists)
        try:
            problem_obj = _get_editable_problem(request.user, problem)
        except Problem.DoesNotExist:
            return JsonResponse({"error": "Problem not found"}, status=404)

        # Clear conversation from cache
        clear_conversation(
            request.user.id,
            problem_obj.id,
            legacy_problem_code=problem_obj.code,
        )

        return JsonResponse({"success": True})


class ChatbotGetHistory(View):
    """API endpoint to get conversation history."""

    def get(self, request, problem):
        # Check authentication
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Not authenticated"}, status=401)
        if not can_use_ai_features(request.user):
            return JsonResponse({"error": "Permission denied"}, status=403)

        # Get problem
        try:
            problem_obj = _get_editable_problem(request.user, problem)
        except Problem.DoesNotExist:
            return JsonResponse({"error": "Problem not found"}, status=404)

        # Get conversation from cache
        conversation = get_conversation(
            request.user.id,
            problem_obj.id,
            legacy_problem_code=problem_obj.code,
        )

        return JsonResponse(
            {
                "success": True,
                "messages": conversation.get("messages", []),
            }
        )


class ChatbotSetModel(View):
    """API endpoint to switch the chatbot model."""

    def post(self, request, problem):
        # Check authentication
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Not authenticated"}, status=401)
        if not can_use_ai_features(request.user):
            return JsonResponse({"error": "Permission denied"}, status=403)

        # Get problem (validate it exists)
        try:
            problem_obj = _get_editable_problem(request.user, problem)
        except Problem.DoesNotExist:
            return JsonResponse({"error": "Problem not found"}, status=404)

        # Get model from request
        model_id = request.POST.get("model", "").strip()
        if not model_id:
            return JsonResponse({"error": "Model is required"}, status=400)

        # Set model in cache
        if set_model(
            request.user.id,
            problem_obj.id,
            model_id,
            legacy_problem_code=problem_obj.code,
        ):
            # Get model name for display
            config = get_config()
            model_name = model_id
            for m in config.get_chatbot_supported_models():
                if m["id"] == model_id:
                    model_name = m["name"]
                    break

            return JsonResponse(
                {
                    "success": True,
                    "model": model_id,
                    "model_name": model_name,
                }
            )
        else:
            return JsonResponse({"error": "Invalid model"}, status=400)


class ChatbotDeleteMessage(View):
    """API endpoint to delete a message from conversation history."""

    def post(self, request, problem):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Not authenticated"}, status=401)
        if not can_use_ai_features(request.user):
            return JsonResponse({"error": "Permission denied"}, status=403)

        try:
            problem_obj = _get_editable_problem(request.user, problem)
        except Problem.DoesNotExist:
            return JsonResponse({"error": "Problem not found"}, status=404)

        try:
            message_index = int(request.POST.get("message_index", -1))
        except (ValueError, TypeError):
            return JsonResponse({"error": "Invalid message index"}, status=400)

        if delete_message(
            request.user.id,
            problem_obj.id,
            message_index,
            legacy_problem_code=problem_obj.code,
        ):
            return JsonResponse({"success": True})
        else:
            return JsonResponse({"error": "Invalid message index"}, status=400)


@short_circuit_middleware
def chatbot_stream_ajax(request):
    """Return partial streaming content for a running chatbot task."""
    task_id = request.GET.get("id")
    if not task_id:
        return JsonResponse({"partial": None, "done": False})

    stream_key = f"{STREAM_CACHE_PREFIX}:{task_id}"
    data = cache.get(stream_key)

    if data is None:
        return JsonResponse({"partial": None, "done": False})

    return JsonResponse(
        {
            "partial": data.get("text", ""),
            "done": data.get("done", False),
        }
    )
