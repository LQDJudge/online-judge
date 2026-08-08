from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from judge.models import Language, Profile
from judge.views.user import UserList


@override_settings(ANON_MAX_PAGE=3, INFINITE_PAGINATION_MAX_PAGE=1000)
class PaginationLimitTest(TestCase):
    fixtures = ["language_small"]

    def assert_redirects_to_login(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)
        self.assertIn("next=", response.url)

    def test_infinite_paginated_querystring_page_is_limited_for_anonymous(self):
        response = self.client.get(reverse("all_submissions"), {"page": "9999999"})

        self.assert_redirects_to_login(response)

    def test_infinite_paginated_path_page_is_limited_for_anonymous(self):
        response = self.client.get(reverse("all_submissions", args=[9999999]))

        self.assert_redirects_to_login(response)

    def test_infinite_paginated_querystring_page_is_limited_for_authenticated_user(
        self,
    ):
        user = User.objects.create_user("page_limit_user", "page@example.com", "pw")
        Profile.objects.create(user=user, language=Language.objects.first())
        self.client.force_login(user)

        response = self.client.get(reverse("all_submissions"), {"page": "1001"})

        self.assertEqual(response.status_code, 404)

    def test_infinite_paginated_path_page_is_limited_for_authenticated_user(self):
        user = User.objects.create_user(
            "path_page_limit_user", "path@example.com", "pw"
        )
        Profile.objects.create(user=user, language=Language.objects.first())
        self.client.force_login(user)

        response = self.client.get(reverse("all_submissions", args=[1001]))

        self.assertEqual(response.status_code, 404)

    def test_user_list_uses_higher_infinite_pagination_limit(self):
        self.assertEqual(UserList().get_infinite_pagination_max_page(), 10000)
