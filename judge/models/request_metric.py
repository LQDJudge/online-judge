from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class RequestMetric(models.Model):
    time = models.DateTimeField(_("time"), default=timezone.now, db_index=True)
    url_name = models.CharField(
        _("URL name"), max_length=255, blank=True, null=True, db_index=True
    )
    path = models.TextField(_("path"), blank=True)
    full_url = models.TextField(_("full URL"), blank=True)
    method = models.CharField(_("method"), max_length=10, blank=True)
    status_code = models.PositiveSmallIntegerField(_("status code"), db_index=True)
    is_authenticated = models.BooleanField(
        _("authenticated"), default=False, db_index=True
    )
    username = models.CharField(
        _("username"), max_length=150, blank=True, db_index=True
    )
    response_time_ms = models.FloatField(_("response time (ms)"), db_index=True)
    db_query_count = models.PositiveIntegerField(
        _("database query count"), blank=True, null=True
    )
    db_time_ms = models.FloatField(_("database time (ms)"), blank=True, null=True)
    cache_call_count = models.PositiveIntegerField(
        _("cache call count"), blank=True, null=True
    )
    cache_time_ms = models.FloatField(_("cache time (ms)"), blank=True, null=True)
    profiler = models.JSONField(_("profiler"), default=dict, blank=True)

    class Meta:
        verbose_name = _("request metric")
        verbose_name_plural = _("request metrics")
        ordering = ["-time"]
        indexes = [
            models.Index(fields=["url_name", "-time"], name="req_metric_url_time"),
            models.Index(fields=["response_time_ms"], name="req_metric_resp_time"),
            models.Index(fields=["time"], name="req_metric_time"),
        ]
