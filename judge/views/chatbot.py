"""
Views for the problem author chatbot feature.
Provides AI-powered assistance for problem authors.
"""

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.views.generic import View
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin

from judge.models import Problem
from judge.views.problem import ProblemMixin, TitleMixin
from judge.chatbot.cache import (
    get_conversation,
    clear_conversation,
    set_model,
)
from llm_service.config import get_config


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
        """Only superusers can access the chatbot."""
        return self.request.user.is_authenticated and self.request.user.is_superuser

    def get(self, request, *args, **kwargs):
        if not self.has_permission():
            from django.http import Http404

            raise Http404()

        self.object = self.get_object()

        # Load conversation history from cache
        conversation = get_conversation(request.user.id, self.object.code)
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

        # Check superuser permission
        if not request.user.is_superuser:
            return JsonResponse({"error": "Permission denied"}, status=403)

        # Get problem
        problem_obj = get_object_or_404(Problem, code=problem)

        # Get message from request
        message = request.POST.get("message", "").strip()
        if not message:
            return JsonResponse({"error": "Message is required"}, status=400)

        # Limit message length
        if len(message) > 10000:
            return JsonResponse(
                {"error": "Message too long (max 10000 characters)"}, status=400
            )

        # Render user message markdown immediately
        from judge.markdown import markdown as render_markdown
        import logging

        logger = logging.getLogger(__name__)

        try:
            user_content_html = render_markdown(message)
        except Exception as md_error:
            logger.warning(f"User message markdown rendering failed: {md_error}")
            user_content_html = (
                '<div class="md-typeset content-description">'
                + escape(message).replace("\n", "<br>")
                + "</div>"
            )

        # Dispatch Celery task
        from judge.tasks.chatbot import chatbot_respond_task

        task = chatbot_respond_task.delay(
            user_id=request.user.id,
            problem_code=problem_obj.code,
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

        # Check superuser permission
        if not request.user.is_superuser:
            return JsonResponse({"error": "Permission denied"}, status=403)

        # Get problem (validate it exists)
        problem_obj = get_object_or_404(Problem, code=problem)

        # Clear conversation from cache
        clear_conversation(request.user.id, problem_obj.code)

        return JsonResponse({"success": True})


class ChatbotGetHistory(View):
    """API endpoint to get conversation history."""

    def get(self, request, problem):
        # Check authentication
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Not authenticated"}, status=401)

        # Check superuser permission
        if not request.user.is_superuser:
            return JsonResponse({"error": "Permission denied"}, status=403)

        # Get problem
        problem_obj = get_object_or_404(Problem, code=problem)

        # Get conversation from cache
        conversation = get_conversation(request.user.id, problem_obj.code)

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

        # Check superuser permission
        if not request.user.is_superuser:
            return JsonResponse({"error": "Permission denied"}, status=403)

        # Get problem (validate it exists)
        problem_obj = get_object_or_404(Problem, code=problem)

        # Get model from request
        model_id = request.POST.get("model", "").strip()
        if not model_id:
            return JsonResponse({"error": "Model is required"}, status=400)

        # Set model in cache
        if set_model(request.user.id, problem_obj.code, model_id):
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
