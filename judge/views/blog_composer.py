from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views import View

from reversion import revisions

from judge.blog_composer.cache import clear_session, get_proposal
from judge.markdown import markdown
from judge.models import BlogPost, Organization, Profile
from judge.tasks.blog_composer import compose_blog_task


def _admin_or_404(request):
    if not request.user.is_authenticated or not request.user.is_superuser:
        raise Http404()


def _post_or_404(post_id):
    if not post_id:
        return None
    try:
        return (
            BlogPost.objects.filter(id=post_id, organizations__is_community=True)
            .distinct()
            .get()
        )
    except BlogPost.DoesNotExist as exc:
        raise Http404() from exc


class BlogComposerView(View):
    def get(self, request):
        _admin_or_404(request)
        query = request.GET.urlencode()
        url = reverse("internal_community_blog_queue") + "?tab=composer"
        return redirect(url + ("&" + query if query else ""))


class BlogComposerSendView(View):
    def post(self, request):
        _admin_or_404(request)
        try:
            post_id = int(request.POST.get("post_id", "") or 0) or None
        except ValueError:
            return JsonResponse({"error": _("Invalid post")}, status=400)
        post = _post_or_404(post_id)
        feedback = request.POST.get("feedback", "").strip()
        if not feedback or len(feedback) > 10000:
            return JsonResponse({"error": _("Feedback is required")}, status=400)
        initial_title = request.POST.get("initial_title", "").strip()
        author_username = request.POST.get("author_username", "").strip()

        organization = None
        if not post:
            try:
                organization = Organization.objects.get(
                    id=int(request.POST.get("organization_id", "")), is_community=True
                )
            except (Organization.DoesNotExist, TypeError, ValueError):
                return JsonResponse({"error": _("Choose an organization")}, status=400)
        else:
            organization = post.organizations.filter(is_community=True).first()

        task = compose_blog_task.delay(
            user_id=request.user.id,
            post_id=post.id if post else None,
            feedback=feedback,
            organization_id=organization.id,
            initial_title=initial_title,
            author_username=author_username,
        )
        return JsonResponse({"task_id": task.id})


class BlogComposerApproveView(View):
    def post(self, request):
        _admin_or_404(request)
        try:
            post_id = int(request.POST.get("post_id", "") or 0) or None
        except ValueError:
            return JsonResponse({"error": _("Invalid post")}, status=400)
        proposal = get_proposal(
            request.user.id, post_id, request.POST.get("proposal_id")
        )
        if not proposal:
            return JsonResponse(
                {"error": _("Proposal is no longer available")}, status=400
            )
        content = request.POST.get("content", proposal["content"])
        if len(content) > 200000:
            return JsonResponse({"error": _("Markdown is too long")}, status=400)

        post = _post_or_404(post_id)
        if post:
            with revisions.create_revision():
                post.title = proposal["title"][:100]
                post.slug = slugify(post.title)[:50]
                post.summary = proposal["summary"]
                post.content = content
                post.publish_on = timezone.now()
                post.save(
                    update_fields=["title", "slug", "summary", "content", "publish_on"]
                )
                revisions.set_user(request.user)
                revisions.set_comment("Approved blog composer proposal")
        else:
            try:
                organization = Organization.objects.get(
                    id=int(request.POST.get("organization_id", "")), is_community=True
                )
            except (Organization.DoesNotExist, TypeError, ValueError):
                return JsonResponse({"error": _("Choose an organization")}, status=400)
            author = Profile.objects.filter(
                user__username=request.POST.get("author_username", "").strip()
            ).first()
            if not author:
                return JsonResponse({"error": _("Choose an author")}, status=400)
            with revisions.create_revision():
                post = BlogPost.objects.create(
                    title=proposal["title"][:100],
                    slug=slugify(proposal["title"])[:50],
                    summary=proposal["summary"],
                    content=content,
                    visible=False,
                    is_rejected=False,
                    is_organization_private=True,
                    publish_on=timezone.now(),
                )
                post.authors.add(author)
                post.organizations.add(organization)
                revisions.set_user(request.user)
                revisions.set_comment("Approved blog composer proposal")

        clear_session(request.user.id, post_id)
        messages.success(request, _("Blog proposal approved."))
        return JsonResponse(
            {"success": True, "url": reverse("internal_community_blog_queue")}
        )


class BlogComposerPreviewView(View):
    def post(self, request):
        _admin_or_404(request)
        try:
            post_id = int(request.POST.get("post_id", "") or 0) or None
        except ValueError:
            return JsonResponse({"error": _("Invalid post")}, status=400)
        proposal = get_proposal(
            request.user.id, post_id, request.POST.get("proposal_id")
        )
        if not proposal:
            return JsonResponse(
                {"error": _("Proposal is no longer available")}, status=400
            )
        content = request.POST.get("content", proposal["content"])
        if len(content) > 200000:
            return JsonResponse({"error": _("Markdown is too long")}, status=400)
        return JsonResponse(
            {"rendered_content": str(markdown(content, lazy_load=False))}
        )


class BlogComposerClearView(View):
    def post(self, request):
        _admin_or_404(request)
        try:
            post_id = int(request.POST.get("post_id", "") or 0) or None
        except ValueError:
            return JsonResponse({"error": _("Invalid post")}, status=400)
        clear_session(request.user.id, post_id)
        return JsonResponse({"success": True})
