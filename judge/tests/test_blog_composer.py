from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from judge.blog_composer.cache import save_proposal
from judge.models import BlogPost, Language, Organization, Profile


class BlogComposerApprovalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python 3",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
                "template": "",
            },
        )
        cls.user = User.objects.create_superuser(
            "composer_admin", "a@example.com", "pw"
        )
        cls.profile, _ = Profile.objects.get_or_create(
            user=cls.user, defaults={"language": cls.language}
        )
        cls.organization = Organization.objects.create(
            name="Composer Community",
            slug="composer-community",
            short_name="Composer",
            registrant=cls.profile,
            is_community=True,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _proposal(self, title="Draft title"):
        return {
            "title": title,
            "summary": "Draft summary",
            "content": "**Tóm tắt:** Draft content.",
        }

    def test_internal_queue_renders_composer_tab(self):
        response = self.client.get(
            reverse("internal_community_blog_queue") + "?tab=composer"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "composer-feedback")

    def test_approval_creates_pending_community_post(self):
        proposal = save_proposal(self.user.id, None, self._proposal())

        response = self.client.post(
            reverse("blog_composer_approve"),
            {"proposal_id": proposal["id"], "organization_id": self.organization.id},
        )

        self.assertEqual(response.status_code, 200)
        post = BlogPost.objects.get(title="Draft title")
        self.assertFalse(post.visible)
        self.assertTrue(post.is_organization_private)
        self.assertEqual(list(post.organizations.all()), [self.organization])
        self.assertEqual(list(post.authors.all()), [self.profile])

    def test_approval_updates_existing_post_without_changing_approval_state(self):
        post = BlogPost.objects.create(
            title="Old title",
            slug="old-title",
            summary="Old summary",
            content="Old content",
            visible=False,
            is_organization_private=True,
            publish_on=timezone.now(),
        )
        post.authors.add(self.profile)
        post.organizations.add(self.organization)
        proposal = save_proposal(self.user.id, post.id, self._proposal("New title"))

        response = self.client.post(
            reverse("blog_composer_approve"),
            {"post_id": post.id, "proposal_id": proposal["id"]},
        )

        self.assertEqual(response.status_code, 200)
        post.refresh_from_db()
        self.assertEqual(post.title, "New title")
        self.assertEqual(post.summary, "Draft summary")
        self.assertFalse(post.visible)
        self.assertEqual(list(post.organizations.all()), [self.organization])
