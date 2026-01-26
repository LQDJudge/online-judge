from itertools import chain, repeat
from operator import itemgetter
import json
import pandas as pd

from django.conf import settings
from django.db.models import Case, Count, FloatField, IntegerField, Value, When
from django.db.models.expressions import CombinedExpression
from django.utils.translation import gettext as _
from django.http import Http404
from django.views.generic import TemplateView
from django.utils.safestring import mark_safe
from django.db import connection

from judge.models import Language, Submission
from judge.utils.stats import (
    chart_colors,
    get_bar_chart,
    get_pie_chart,
    highlight_colors,
)


class StatViewBase(TemplateView):
    def get(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            raise Http404
        return super().get(request, *args, **kwargs)


class StatLanguage(StatViewBase):
    template_name = "stats/language.html"
    ac_count = Count(
        Case(When(submission__result="AC", then=Value(1)), output_field=IntegerField())
    )

    def repeat_chain(iterable):
        return chain.from_iterable(repeat(iterable))

    def language_data(
        self, language_count=Language.objects.annotate(count=Count("submission"))
    ):
        languages = (
            language_count.filter(count__gt=0)
            .values("key", "name", "count")
            .order_by("-count")
        )
        num_languages = min(len(languages), settings.DMOJ_STATS_LANGUAGE_THRESHOLD)
        other_count = sum(map(itemgetter("count"), languages[num_languages:]))

        return {
            "labels": list(map(itemgetter("name"), languages[:num_languages]))
            + ["Other"],
            "datasets": [
                {
                    "backgroundColor": chart_colors[:num_languages] + ["#FDB45C"],
                    "highlightBackgroundColor": highlight_colors[:num_languages]
                    + ["#FFC870"],
                    "data": list(map(itemgetter("count"), languages[:num_languages]))
                    + [other_count],
                },
            ],
        }

    def ac_language_data(self):
        return self.language_data(Language.objects.annotate(count=self.ac_count))

    def status_data(self, statuses=None):
        if not statuses:
            statuses = (
                Submission.objects.values("result")
                .annotate(count=Count("result"))
                .values("result", "count")
                .order_by("-count")
            )
        data = []
        for status in statuses:
            res = status["result"]
            if not res:
                continue
            count = status["count"]
            data.append((str(Submission.USER_DISPLAY_CODES[res]), count))

        return get_pie_chart(data)

    def ac_rate(self):
        rate = CombinedExpression(
            self.ac_count / Count("submission"),
            "*",
            Value(100.0),
            output_field=FloatField(),
        )
        data = (
            Language.objects.annotate(total=Count("submission"), ac_rate=rate)
            .filter(total__gt=0)
            .order_by("total")
            .values_list("name", "ac_rate")
        )
        return get_bar_chart(list(data))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Language statistics")
        context["tab"] = "language"
        context["data_all"] = mark_safe(json.dumps(self.language_data()))
        context["lang_ac"] = mark_safe(json.dumps(self.ac_language_data()))
        context["status_counts"] = mark_safe(json.dumps(self.status_data()))
        context["ac_rate"] = mark_safe(json.dumps(self.ac_rate()))
        return context


def group_data_by_period(dates, data, period="1W"):
    df = pd.DataFrame()
    df["dates"] = pd.to_datetime(dates)
    df["data"] = data
    df = df.groupby([pd.Grouper(key="dates", freq=period)])["data"].mean().reset_index()
    return list(df["dates"]), list(df["data"])


class StatSite(StatViewBase):
    template_name = "stats/site.html"

    def create_dataset(self, query, label):
        labels = []
        data = []
        with connection.cursor() as cursor:
            cursor.execute(query)
            for row in cursor.fetchall():
                data.append(row[0])
                labels.append(row[1])

        labels, data = group_data_by_period(labels, data, self.period)

        res = {
            "labels": labels,
            "datasets": [
                {
                    "label": label,
                    "data": data,
                    "borderColor": "rgb(75, 192, 192)",
                    "pointRadius": 1,
                    "borderWidth": 2,
                }
            ],
        }
        return mark_safe(json.dumps(res, default=str))

    def get_submissions(self):
        query = """
        SELECT COUNT(*), CAST(date AS Date) as day from judge_submission GROUP BY day; 
        """
        return self.create_dataset(query, _("Submissions"))

    def get_comments(self):
        query = """
        SELECT COUNT(*), CAST(time AS Date) as day from judge_comment GROUP BY day; 
        """
        return self.create_dataset(query, _("Comments"))

    def get_new_users(self):
        query = """
        SELECT COUNT(*), CAST(date_joined AS Date) as day from auth_user GROUP BY day; 
        """
        return self.create_dataset(query, _("New users"))

    def get_chat_messages(self):
        query = """
        SELECT COUNT(*), CAST(time AS Date) as day from chat_box_message GROUP BY day; 
        """
        return self.create_dataset(query, _("Chat messages"))

    def get_contests(self):
        query = """
        SELECT COUNT(*), CAST(start_time AS Date) as day from judge_contest GROUP BY day; 
        """
        return self.create_dataset(query, _("Contests"))

    def get_groups(self):
        query = """
        SELECT COUNT(*), CAST(creation_date AS Date) as day from judge_organization GROUP BY day; 
        """
        return self.create_dataset(query, _("Groups"))

    def get(self, request, *args, **kwargs):
        self.period = request.GET.get("period", "1W")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tab"] = "site"
        context["title"] = _("Site statistics")
        context["submissions"] = self.get_submissions()
        context["comments"] = self.get_comments()
        context["new_users"] = self.get_new_users()
        context["chat_messages"] = self.get_chat_messages()
        context["contests"] = self.get_contests()
        context["groups"] = self.get_groups()
        return context
