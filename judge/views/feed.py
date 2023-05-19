from django.views.generic import ListView
from django.shortcuts import render
from django.urls import reverse

from judge.utils.infinite_paginator import InfinitePaginationMixin


class FeedView(InfinitePaginationMixin, ListView):
    def get_feed_context(selfl, object_list):
        return {}

    def get(self, request, *args, **kwargs):
        only_content = request.GET.get("only_content", None)
        if only_content and self.feed_content_template_name:
            queryset = self.get_queryset()
            paginator, page, object_list, _ = self.paginate_queryset(
                queryset, self.paginate_by
            )
            context = {
                self.context_object_name: object_list,
                "has_next_page": page.has_next(),
            }
            context.update(self.get_feed_context(object_list))
            return render(request, self.feed_content_template_name, context)

        return super(FeedView, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["has_next_page"] = context["page_obj"].has_next()
        try:
            context["feed_content_url"] = reverse(self.url_name)
        except Exception as e:
            context["feed_content_url"] = self.request.path
        return context
