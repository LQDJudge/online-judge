import math

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class AIUsageLog(models.Model):
    CHARS_PER_ESTIMATED_TOKEN = 4

    FEATURE_LABELS = {
        "chat_moderation": _("Chat moderation"),
        "comment_moderation": _("Comment moderation"),
        "community_blog_composer": _("Community blog composer"),
        "contest_review_synthesis": _("Contest review synthesis"),
        "magazine_post_generation": _("Magazine post generation"),
        "problem_ai": _("Problem AI"),
        "problem_chatbot": _("Problem chatbot"),
        "problem_markdown": _("Problem markdown"),
        "problem_review_check": _("Problem review check"),
        "problem_review_synthesis": _("Problem review synthesis"),
        "problem_tagging": _("Problem tagging"),
        "profile_moderation": _("Profile moderation"),
        "quiz_ai": _("Quiz AI"),
        "quiz_import": _("Quiz import"),
        "solution_code_generation": _("Solution code generation"),
        "solution_generation": _("Solution generation"),
        "username_moderation": _("Username moderation"),
    }

    STATUS_SUCCESS = "success"
    STATUS_ERROR = "error"
    STATUS_TIMEOUT = "timeout"

    STATUS_CHOICES = (
        (STATUS_SUCCESS, _("Success")),
        (STATUS_ERROR, _("Error")),
        (STATUS_TIMEOUT, _("Timeout")),
    )

    time = models.DateTimeField(_("time"), default=timezone.now, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="ai_usage_logs",
    )
    username = models.CharField(
        _("username"), max_length=150, blank=True, db_index=True
    )
    feature = models.CharField(_("feature"), max_length=100, blank=True, db_index=True)
    bot_name = models.CharField(
        _("bot name"), max_length=100, blank=True, db_index=True
    )
    status = models.CharField(
        _("status"), max_length=20, choices=STATUS_CHOICES, db_index=True
    )
    duration_ms = models.FloatField(_("duration (ms)"), blank=True, null=True)
    input_chars = models.PositiveIntegerField(_("input chars"), default=0)
    output_chars = models.PositiveIntegerField(_("output chars"), default=0)
    message_count = models.PositiveIntegerField(_("message count"), default=0)
    attachment_count = models.PositiveIntegerField(_("attachment count"), default=0)
    tool_count = models.PositiveIntegerField(_("tool count"), default=0)
    error = models.TextField(_("error"), blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        verbose_name = _("AI usage log")
        verbose_name_plural = _("AI usage logs")
        ordering = ["-time"]
        indexes = [
            models.Index(fields=["feature", "-time"], name="ai_usage_feature_time"),
            models.Index(fields=["username", "-time"], name="ai_usage_user_time"),
            models.Index(fields=["bot_name", "-time"], name="ai_usage_bot_time"),
            models.Index(fields=["status", "-time"], name="ai_usage_status_time"),
        ]

    @classmethod
    def estimate_tokens(cls, char_count):
        return math.ceil((char_count or 0) / cls.CHARS_PER_ESTIMATED_TOKEN)

    @property
    def estimated_input_tokens(self):
        return self.estimate_tokens(self.input_chars)

    @property
    def estimated_output_tokens(self):
        return self.estimate_tokens(self.output_chars)

    @property
    def feature_label(self):
        return self.get_feature_label(self.feature)

    @classmethod
    def get_feature_label(cls, feature):
        return cls.FEATURE_LABELS.get(feature, feature)
