from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views.generic import UpdateView, View
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin

from reversion import revisions

from judge.forms import BlogPostEditForm
from judge.models import BlogPost
from judge.utils.feed import build_home_feed
from judge.utils.views import TitleMixin, generic_message
from judge.views.bookmark import BookMarkDetailView
from judge.views.comment import CommentableMixin
from judge.views.feed import HomeFeedView
from judge.views.pagevote import PageVoteDetailView


class PostList(HomeFeedView):
    model = BlogPost
    paginate_by = 4
    context_object_name = "posts"
    feed_content_template_name = "home/feed-content.html"
    url_name = "blog_post_list"

    def get(self, request, *args, **kwargs):
        # For logged-in users: cursor-based mixed feed
        if request.user.is_authenticated:
            only_content = request.GET.get("only_content")
            cursor_str = request.GET.get("cursor")

            self.ensure_feed_token(request)
            feed_result = build_home_feed(request, cursor_str=cursor_str)

            if only_content and self.feed_content_template_name:
                # AJAX infinite scroll
                context = {
                    "feed_items": feed_result["items"],
                    "has_next_page": feed_result["has_next_page"],
                    "next_cursor": feed_result["next_cursor"],
                }
                return render(request, self.feed_content_template_name, context)

            # Full page load — need sidebar context from HomeFeedView
            self.feed_result = feed_result
            self.page = 1
            return super(HomeFeedView, self).get(request, *args, **kwargs)

        # For logged-out users: simple post pagination (old behavior)
        only_content = request.GET.get("only_content")
        if only_content and request.GET.get("cursor") and not request.GET.get("page"):
            return render(
                request,
                "blog/content.html",
                {
                    "posts": [],
                    "has_next_page": False,
                    "show_organization_private_icon": True,
                },
            )
        if only_content:
            self.feed_content_template_name = "blog/content.html"
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        """Queryset for logged-out users (simple post pagination)."""
        queryset = BlogPost.objects.filter(
            visible=True,
            publish_on__lte=timezone.now(),
            is_organization_private=False,
        )
        return queryset.order_by("-sticky", "-publish_on")

    def get_context_data(self, **kwargs):
        context = super(PostList, self).get_context_data(**kwargs)
        context["title"] = self.title or _("Home")
        context["page_type"] = "blog"
        context["show_organization_private_icon"] = True

        # For logged-in users: use pre-built feed result
        if hasattr(self, "feed_result") and self.feed_result:
            context["feed_items"] = self.feed_result["items"]
            context["has_next_page"] = self.feed_result["has_next_page"]
            context["next_cursor"] = self.feed_result["next_cursor"]
        else:
            # Logged-out: prefetch for blog/content.html
            BlogPost.prefetch_organization_ids(*[post.id for post in context["posts"]])

        return context

    def get_feed_context(self, object_list):
        """For logged-out AJAX pagination only."""
        context = {}
        context["show_organization_private_icon"] = True
        BlogPost.prefetch_organization_ids(*[post.id for post in object_list])
        return context


class PostView(
    TitleMixin,
    CommentableMixin,
    PageVoteDetailView,
    BookMarkDetailView,
    TemplateResponseMixin,
    SingleObjectMixin,
    View,
):
    model = BlogPost
    pk_url_kwarg = "id"
    context_object_name = "post"
    template_name = "blog/blog.html"

    def get_title(self):
        return self.object.title

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return self.render_to_response(
            self.get_context_data(
                object=self.object,
            )
        )

    def get_context_data(self, **kwargs):
        context = super(PostView, self).get_context_data(**kwargs)
        context["og_image"] = self.object.og_image
        context["editable_orgs"] = []

        context["organizations"] = self.object.get_organizations()

        if self.request.profile:
            is_author = self.request.profile.id in self.object.get_author_ids()
            for org in context["organizations"]:
                if self.request.profile.can_edit_organization(org):
                    context["editable_orgs"].append(org)
                elif org.can_moderate(self.request.profile):
                    context["editable_orgs"].append(org)
                elif is_author and not self.object.visible:
                    context["editable_orgs"].append(org)

        if self.object.is_editable_by(self.request.user):
            context["num_open_tickets"] = self.object.tickets.filter(
                is_open=True
            ).count()

        context = self.get_comment_context(context)

        return context

    def get_object(self, queryset=None):
        post = super(PostView, self).get_object(queryset)
        if not post.is_accessible_by(self.request.user):
            raise Http404()
        return post


class EditBlogPost(LoginRequiredMixin, TitleMixin, UpdateView):
    model = BlogPost
    pk_url_kwarg = "id"
    template_name = "blog/edit.html"
    form_class = BlogPostEditForm

    def get_title(self):
        return _("Edit %s") % self.object.title

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        self.object = self.get_object()
        if self.object.is_organization_private:
            return generic_message(
                request,
                _("Use organization edit"),
                _(
                    "This post is private to an organization. "
                    "Edit it through the organization page."
                ),
                status=400,
            )
        if not self.object.is_editable_by(request.user):
            return generic_message(
                request,
                _("Permission denied"),
                _("You are not allowed to edit this blog post."),
                status=403,
            )
        if request.profile.mute and not request.user.is_superuser:
            return generic_message(
                request,
                _("Muted"),
                _("Muted users are not allowed to edit blog posts."),
                status=403,
            )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["is_admin"] = self.request.user.is_superuser
        return kwargs

    def form_valid(self, form):
        # Slug must be regenerated before super().form_valid so that
        # get_success_url builds the redirect with the new slug.
        form.instance.slug = slugify(form.instance.title)[:50]
        with revisions.create_revision():
            res = super().form_valid(form)
            # Privacy is derived from org membership: a post is org-private
            # iff it has any organizations attached.
            new_state = self.object.organizations.exists()
            if self.object.is_organization_private != new_state:
                self.object.is_organization_private = new_state
                self.object.save()
            revisions.set_comment(_("Edited from site"))
            revisions.set_user(self.request.user)
        messages.success(self.request, _("Blog post updated."))
        return res

    def get_success_url(self):
        return reverse("blog_post", args=[self.object.id, self.object.slug])
