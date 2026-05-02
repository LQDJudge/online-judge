from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import BlogPost, Language, Organization, Profile


class BlogEditBase(TestCase):
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

    def setUp(self):
        self.author_user = User.objects.create_user(
            username="author", password="password123"
        )
        self.author_profile, _ = Profile.objects.get_or_create(
            user=self.author_user, defaults={"language": self.language}
        )

        self.other_user = User.objects.create_user(
            username="other", password="password123"
        )
        self.other_profile, _ = Profile.objects.get_or_create(
            user=self.other_user, defaults={"language": self.language}
        )

        self.super_user = User.objects.create_superuser(
            username="superuser", password="password123", email="su@test.local"
        )
        self.super_profile, _ = Profile.objects.get_or_create(
            user=self.super_user, defaults={"language": self.language}
        )

        self.muted_user = User.objects.create_user(
            username="muted", password="password123"
        )
        self.muted_profile, _ = Profile.objects.get_or_create(
            user=self.muted_user, defaults={"language": self.language}
        )
        self.muted_profile.mute = True
        self.muted_profile.save()

        self.client = Client()

    def _make_post(
        self, *, visible=True, is_org_private=False, authors=None, title="My Post"
    ):
        post = BlogPost.objects.create(
            title=title,
            slug="my-post",
            visible=visible,
            sticky=False,
            publish_on=timezone.now(),
            content="Hello",
            is_organization_private=is_org_private,
        )
        if authors:
            for profile in authors:
                post.authors.add(profile)
        return post

    def _edit_url(self, post):
        return reverse("edit_blog_post", args=[post.id, post.slug])


class BlogEditPermissionTests(BlogEditBase):
    def test_anonymous_user_redirected_to_login(self):
        post = self._make_post(authors=[self.author_profile])
        response = self.client.get(self._edit_url(post))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_non_author_non_superuser_gets_403(self):
        post = self._make_post(authors=[self.author_profile])
        self.client.login(username="other", password="password123")
        response = self.client.get(self._edit_url(post))
        self.assertEqual(response.status_code, 403)

    def test_author_can_access_form(self):
        post = self._make_post(authors=[self.author_profile])
        self.client.login(username="author", password="password123")
        response = self.client.get(self._edit_url(post))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, post.title)

    def test_superuser_non_author_can_access_form(self):
        post = self._make_post(authors=[self.author_profile])
        self.client.login(username="superuser", password="password123")
        response = self.client.get(self._edit_url(post))
        self.assertEqual(response.status_code, 200)

    def test_muted_author_blocked(self):
        post = self._make_post(authors=[self.muted_profile])
        self.client.login(username="muted", password="password123")
        response = self.client.get(self._edit_url(post))
        self.assertEqual(response.status_code, 403)

    def test_muted_superuser_bypasses_mute(self):
        # A superuser can be muted but should still edit.
        self.super_profile.mute = True
        self.super_profile.save()
        post = self._make_post(authors=[self.author_profile])
        self.client.login(username="superuser", password="password123")
        response = self.client.get(self._edit_url(post))
        self.assertEqual(response.status_code, 200)

    def test_org_private_post_returns_400(self):
        post = self._make_post(authors=[self.author_profile], is_org_private=True)
        self.client.login(username="author", password="password123")
        response = self.client.get(self._edit_url(post))
        self.assertEqual(response.status_code, 400)


class BlogEditSaveTests(BlogEditBase):
    def test_post_updates_title_and_content(self):
        post = self._make_post(authors=[self.author_profile])
        self.client.login(username="author", password="password123")
        response = self.client.post(
            self._edit_url(post),
            {"title": "New Title", "content": "Updated body", "visible": "on"},
        )
        self.assertEqual(response.status_code, 302)
        post.refresh_from_db()
        self.assertEqual(post.title, "New Title")
        self.assertEqual(post.content, "Updated body")

    def test_slug_regenerates_from_title(self):
        post = self._make_post(authors=[self.author_profile], title="Old")
        self.client.login(username="author", password="password123")
        self.client.post(
            self._edit_url(post),
            {"title": "Brand New Title", "content": "x", "visible": "on"},
        )
        post.refresh_from_db()
        self.assertEqual(post.slug, "brand-new-title")

    def test_redirect_uses_new_slug(self):
        post = self._make_post(authors=[self.author_profile])
        self.client.login(username="author", password="password123")
        response = self.client.post(
            self._edit_url(post),
            {"title": "Different Heading", "content": "x", "visible": "on"},
        )
        post.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse("blog_post", args=[post.id, "different-heading"]),
        )


class BlogEditVisibilityTests(BlogEditBase):
    def test_author_can_demote_visible_post(self):
        post = self._make_post(authors=[self.author_profile], visible=True)
        self.client.login(username="author", password="password123")
        # Omit `visible` in POST -> checkbox False -> demote.
        self.client.post(
            self._edit_url(post),
            {"title": post.title, "content": "x"},
        )
        post.refresh_from_db()
        self.assertFalse(post.visible)

    def test_hidden_post_renders_disabled_visible_field_for_author(self):
        post = self._make_post(authors=[self.author_profile], visible=False)
        self.client.login(username="author", password="password123")
        response = self.client.get(self._edit_url(post))
        self.assertEqual(response.status_code, 200)
        # Django renders disabled inputs with the `disabled` attribute.
        self.assertContains(response, 'name="visible"')
        self.assertContains(response, "disabled")

    def test_author_cannot_promote_hidden_post(self):
        post = self._make_post(authors=[self.author_profile], visible=False)
        self.client.login(username="author", password="password123")
        # Even with visible=on in POST, Django ignores POST data for disabled
        # fields, so the form's cleaned visible stays False and the post
        # remains hidden.
        self.client.post(
            self._edit_url(post),
            {"title": post.title, "content": "x", "visible": "on"},
        )
        post.refresh_from_db()
        self.assertFalse(post.visible)

    def test_clean_validator_rejects_bypassed_promotion(self):
        # Direct form construction simulates a bypass of the disabled-field UI:
        # if a tampered POST got past Django's disabled-field handling, the
        # clean() validator must still reject the promotion.
        from judge.forms import BlogPostEditForm

        post = self._make_post(authors=[self.author_profile], visible=False)
        form = BlogPostEditForm(
            data={"title": post.title, "content": "x", "visible": True},
            instance=post,
            is_admin=False,
        )
        # Re-enable the field on the form after construction to simulate
        # the bypass.
        form.fields["visible"].disabled = False
        self.assertFalse(form.is_valid())
        self.assertIn("__all__", form.errors)


class BlogEditAdminFieldsTests(BlogEditBase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(
            name="Test Org",
            slug="test-org",
            short_name="TO",
            about="x",
            registrant=self.super_profile,
            is_open=True,
        )

    def test_admin_sees_organizations_field(self):
        post = self._make_post(authors=[self.author_profile])
        self.client.login(username="superuser", password="password123")
        response = self.client.get(self._edit_url(post))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="organizations"')
        # is_organization_private is intentionally NOT exposed; it is derived
        # from organizations on save.
        self.assertNotContains(response, 'name="is_organization_private"')

    def test_non_admin_does_not_see_organizations_field(self):
        post = self._make_post(authors=[self.author_profile])
        self.client.login(username="author", password="password123")
        response = self.client.get(self._edit_url(post))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="organizations"')

    def test_attaching_org_makes_post_organization_private(self):
        # Auto-derive: adding orgs flips is_organization_private to True.
        post = self._make_post(authors=[self.author_profile])
        self.assertFalse(post.is_organization_private)
        self.client.login(username="superuser", password="password123")
        response = self.client.post(
            self._edit_url(post),
            {
                "title": post.title,
                "content": "x",
                "visible": "on",
                "organizations": [self.org.id],
            },
        )
        # Note: this redirects to the post detail URL — but since the post is
        # now organization-private, the new edit page would 400 on next visit.
        self.assertEqual(response.status_code, 302)
        post.refresh_from_db()
        self.assertIn(self.org, post.organizations.all())
        self.assertTrue(post.is_organization_private)

    def test_removing_all_orgs_makes_post_public(self):
        # Auto-derive: removing all orgs flips is_organization_private to False.
        # Setup: a post with an org attached + boolean True. To get to this
        # state we need a public post (so the new edit page accepts it) — so
        # we attach the org without setting the boolean, then assert via edit.
        post = self._make_post(authors=[self.author_profile])
        post.organizations.add(self.org)
        post.is_organization_private = True
        post.save()
        # is_accessible_by would now reject — but EditBlogPost's dispatch
        # rejects org-private posts at the gate, so this state is not
        # editable via the new page anyway. To exercise the auto-derive
        # branch we need to start from a state the new page accepts: orgs
        # attached but is_organization_private=False (the cross-listed
        # state, which doesn't occur naturally but the form can reach it
        # transiently if the boolean were toggled by admin).
        post.is_organization_private = False
        post.save()
        self.client.login(username="superuser", password="password123")
        response = self.client.post(
            self._edit_url(post),
            {
                "title": post.title,
                "content": "x",
                "visible": "on",
                "organizations": [],
            },
        )
        self.assertEqual(response.status_code, 302)
        post.refresh_from_db()
        self.assertEqual(list(post.organizations.all()), [])
        self.assertFalse(post.is_organization_private)

    def test_superuser_can_promote_hidden_post(self):
        post = self._make_post(authors=[self.author_profile], visible=False)
        self.client.login(username="superuser", password="password123")
        response = self.client.post(
            self._edit_url(post),
            {"title": post.title, "content": "x", "visible": "on"},
        )
        self.assertEqual(response.status_code, 302)
        post.refresh_from_db()
        self.assertTrue(post.visible)


class BlogEditStaleSlugTests(BlogEditBase):
    def test_stale_slug_in_url_resolves_correct_post(self):
        post = self._make_post(authors=[self.author_profile])
        self.client.login(username="author", password="password123")
        stale_url = reverse("edit_blog_post", args=[post.id, "wrong-slug"])
        response = self.client.get(stale_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, post.title)


class BlogPostDetailEditButtonTests(BlogEditBase):
    def test_author_sees_new_edit_link_on_detail_page(self):
        post = self._make_post(authors=[self.author_profile])
        self.client.login(username="author", password="password123")
        response = self.client.get(reverse("blog_post", args=[post.id, post.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, reverse("edit_blog_post", args=[post.id, post.slug])
        )
        self.assertNotContains(response, f"/admin/judge/blogpost/{post.id}/change/")

    def test_non_author_sees_no_edit_button(self):
        post = self._make_post(authors=[self.author_profile])
        self.client.login(username="other", password="password123")
        response = self.client.get(reverse("blog_post", args=[post.id, post.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response, reverse("edit_blog_post", args=[post.id, post.slug])
        )


class OrgEditFieldGatingTests(BlogEditBase):
    """Verify the organizations field on the org Edit page is gated to
    superusers only, and that the auto-derive of is_organization_private
    fires on save through that page."""

    def setUp(self):
        super().setUp()
        # Non-superuser org admin (distinct from site superuser).
        self.org_admin_user = User.objects.create_user(
            username="orgadmin", password="password123"
        )
        self.org_admin_profile, _ = Profile.objects.get_or_create(
            user=self.org_admin_user, defaults={"language": self.language}
        )
        self.org = Organization.objects.create(
            name="Edit Test Org",
            slug="edit-test-org",
            short_name="ETO",
            about="x",
            registrant=self.org_admin_profile,
            is_open=True,
        )
        self.org.admins.add(self.org_admin_profile)
        self.org.members.add(self.org_admin_profile, self.author_profile)
        # Org-private post inside the org.
        self.post = self._make_post(authors=[self.author_profile], is_org_private=True)
        self.post.organizations.add(self.org)
        self.post.save()

    def _org_edit_url(self):
        return reverse(
            "edit_organization_blog",
            args=[self.org.id, self.org.slug, self.post.id],
        )

    def test_superuser_sees_organizations_field_on_org_edit(self):
        self.client.login(username="superuser", password="password123")
        response = self.client.get(self._org_edit_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="organizations"')

    def test_org_admin_does_not_see_organizations_field(self):
        self.client.login(username="orgadmin", password="password123")
        response = self.client.get(self._org_edit_url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="organizations"')

    def test_superuser_remove_orgs_auto_derives_public(self):
        # Superuser removes all orgs via the org Edit page.
        # is_organization_private should auto-flip to False.
        self.client.login(username="superuser", password="password123")
        response = self.client.post(
            self._org_edit_url(),
            {
                "title": self.post.title,
                "content": "x",
                "visible": "on",
                "organizations": [],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.post.refresh_from_db()
        self.assertEqual(list(self.post.organizations.all()), [])
        self.assertFalse(self.post.is_organization_private)
