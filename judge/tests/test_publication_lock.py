from datetime import timedelta
from html.parser import HTMLParser
from io import StringIO
import json
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

from bs4 import BeautifulSoup

from django.contrib.auth.models import Permission, User
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.test import RequestFactory, TestCase, override_settings
from django.templatetags.static import static
from django.utils import timezone
from django.urls import reverse

from judge.content_mutation import (
    ContentMutationMiddleware,
    mutation_target,
    publication_confirmation_token,
)
from judge.admin.problem import ProblemAdmin
from judge.admin.contest import ContestAdmin, ContestPublicRequestAdmin
from judge.admin.interface import BlogPostAdmin
from judge.admin.quiz import QuizAdmin, QuizQuestionAdmin
from judge.models import (
    Language,
    LanguageLimit,
    LanguageTemplate,
    Problem,
    ProblemGroup,
    ProblemReviewRun,
    ProblemTranslation,
    Profile,
    PublicationRequestEvent,
    PublicRequest,
    Quiz,
    QuizAttempt,
    QuizQuestion,
    Submission,
    Solution,
    BlogPost,
    Contest,
    ContestPublicRequest,
    ContestProblem,
    ContestReviewRun,
)
from judge.review.publication import (
    decide_publication,
    submit_publication,
    withdraw_publication,
    publish_without_review,
)
from judge.views.contest_review import (
    _contest_sidebar_context,
    contest_review_accept,
    contest_request_cancel,
)
from judge.views.contests import can_view_contest_clarifications
from judge.views.api.api_v1 import api_v1_contest_detail
from judge.views.problem_data import ProblemManagerMixin
from judge.views.internal import publish_problem
from judge.views.problem import (
    ProblemEdit,
    ProblemEditLanguageLimits,
    ProblemEditLanguageTemplates,
    ProblemEditSolutions,
    ProblemEditTranslations,
)
from judge.views.resolver import Resolver
from judge.utils.hidden_results import hidden_result_problem_ids
from judge.views.content_lock import toggle_content_lock
from judge.review.exposure import (
    contest_publication_exposure,
    publication_access_details,
    publication_exposure_concern,
)
from judge.models.notification import Notification, NotificationCategory
from judge.review.snapshots import build_contest_snapshot, build_problem_manifest


class ConfirmationInputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.fields = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("type") == "hidden":
            self.fields.append((attrs.get("name"), attrs.get("value", "")))


@override_settings(AUTO_REVIEW_COORDINATOR_ENABLED=True, LANGUAGE_CODE="en")
class PublicationLockTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        language, _ = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python 3",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
            },
        )
        group, _ = ProblemGroup.objects.get_or_create(
            name="locktests", defaults={"full_name": "Lock tests"}
        )
        cls.author = User.objects.create_user("publication-author", password="pw")
        cls.author.user_permissions.add(Permission.objects.get(codename="add_problem"))
        cls.profile, _ = Profile.objects.get_or_create(
            user=cls.author, defaults={"language": language}
        )
        cls.admin = User.objects.create_superuser(
            "publication-admin", "admin@example.org", "pw"
        )
        cls.admin_profile, _ = Profile.objects.get_or_create(
            user=cls.admin, defaults={"language": language}
        )
        cls.problem = Problem.objects.create(
            code="locktest",
            name="Lock test",
            description="Original",
            group=group,
            time_limit=1,
            memory_limit=65536,
            points=1,
        )
        cls.problem.authors.add(cls.profile)
        cls.review_run = ProblemReviewRun.objects.create(
            problem=cls.problem,
            triggered_by=cls.profile,
            status=ProblemReviewRun.DONE,
            workflow_version=2,
            content_hash="a" * 64,
            input_hash="b" * 64,
            readiness="READY",
        )
        # This is the previously completed review. Give it an explicit earlier
        # timestamp so latest-candidate tests do not depend on clock precision
        # or wall-clock corrections while the suite is running.
        ProblemReviewRun.objects.filter(pk=cls.review_run.pk).update(
            started_at=timezone.now() - timedelta(days=1)
        )
        cls.review_run.refresh_from_db()

    def setUp(self):
        # Cache invalidations survive TestCase's database rollback. In
        # particular the removed-editor test must not poison later fixtures.
        cache.clear()
        self.addCleanup(cache.clear)
        self.hash_patch = patch(
            "judge.review.publication.workflow.current_content_hash",
            return_value="a" * 64,
        )
        self.ready_patch = patch(
            "judge.review.publication.workflow.publication_eligibility",
            return_value=True,
        )
        self.blockers_patch = patch(
            "judge.review.publication.workflow.publication_blockers", return_value=[]
        )
        self.hash_patch.start()
        self.ready_patch.start()
        self.blockers_patch.start()
        self.addCleanup(self.hash_patch.stop)
        self.addCleanup(self.ready_patch.stop)
        self.addCleanup(self.blockers_patch.stop)

    def submit(self):
        return submit_publication(self.problem, self.author, self.review_run.pk)

    def content_payload(self, **changes):
        data = {
            "code": self.problem.code,
            "name": self.problem.name,
            "description": self.problem.description,
            "group": self.problem.group_id,
            "authors": [self.profile.pk],
            "time_limit": 1,
            "memory_limit": 64,
            "memory_unit": "MB",
            "points": 1,
            "allowed_languages": [self.profile.language_id],
        }
        data.update(changes)
        return data

    def test_content_visibility_and_lock_controls_are_superuser_only(self):
        self.client.force_login(self.admin)
        for public in (False, True):
            Problem.objects.filter(pk=self.problem.pk).update(is_public=public)
            Problem.dirty_cache(self.problem.pk)
            response = self.client.get("/problem/locktest/edit")
            self.assertEqual(response.status_code, 200)
            form = response.context["form"]
            self.assertFalse(form.fields["is_public"].disabled)
            self.assertIn("is_locked", form.fields)
            self.assertContains(response, 'name="is_locked"')
        Problem.objects.filter(pk=self.problem.pk).update(is_public=False)
        Problem.dirty_cache(self.problem.pk)
        self.client.force_login(self.author)
        response = self.client.get("/problem/locktest/edit")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].fields["is_public"].disabled)
        self.assertNotIn("is_locked", response.context["form"].fields)
        self.assertNotContains(response, 'name="is_locked"')

    def test_superuser_can_lock_and_unlock_private_content_via_save(self):
        self.client.force_login(self.admin)
        for locked in (True, False):
            response = self.client.post(
                "/problem/locktest/edit",
                self.content_payload(is_locked="on" if locked else ""),
            )
            self.assertEqual(response.status_code, 302)
            self.problem.refresh_from_db()
            self.assertEqual(self.problem.is_locked, locked)
            self.assertFalse(self.problem.is_public)

    def test_direct_content_publication_is_audited_and_auto_locks(self):
        self.client.force_login(self.admin)
        pending = self.submit()
        response = self.client.post(
            "/problem/locktest/edit", self.content_payload(is_public="on")
        )
        self.assertEqual(response.status_code, 302)
        self.problem.refresh_from_db()
        pending.refresh_from_db()
        self.assertTrue(self.problem.is_public)
        self.assertTrue(self.problem.is_locked)
        self.assertEqual(pending.status, PublicRequest.APPROVED)
        self.assertEqual(
            Notification.objects.filter(
                owner=self.profile,
                category=NotificationCategory.PUBLIC_REQUEST_APPROVED,
            ).count(),
            1,
        )
        self.assertTrue(
            PublicationRequestEvent.objects.filter(
                problem=self.problem,
                actor=self.admin_profile,
                status=PublicRequest.APPROVED,
                reason="Published directly by a superuser from Content.",
            ).exists()
        )
        response = self.client.post(
            "/problem/locktest/edit", self.content_payload(is_locked="on")
        )
        self.assertEqual(response.status_code, 302)
        self.problem.refresh_from_db()
        self.assertFalse(self.problem.is_public)
        self.assertTrue(self.problem.is_locked)

    def test_author_cannot_forge_visibility_or_lock_on_content_save(self):
        self.client.force_login(self.author)
        response = self.client.post(
            "/problem/locktest/edit",
            self.content_payload(is_public="on", is_locked="on"),
        )
        self.assertEqual(response.status_code, 302)
        self.problem.refresh_from_db()
        self.assertFalse(self.problem.is_public)
        self.assertFalse(self.problem.is_locked)

    def test_request_binds_exact_run_and_is_idempotent(self):
        first = self.submit()
        second = self.submit()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.review_run_id, self.review_run.pk)
        self.assertEqual(first.candidate_hash, "a" * 64)
        self.assertEqual(PublicationRequestEvent.objects.count(), 1)
        self.assertEqual(ProblemReviewRun.objects.count(), 1)

    def test_historical_candidate_survives_metadata_edit_but_not_statement_change(self):
        self.hash_patch.stop()
        self.review_run.snapshot = build_problem_manifest(self.problem)
        self.review_run.save(update_fields=["snapshot"])
        row = self.submit()
        self.problem.name = "Renamed"
        self.problem.date = timezone.now()
        self.problem.save(update_fields=["name", "date"])
        self.assertEqual(self.submit().pk, row.pk)
        self.problem.description = "Different rule"
        self.problem.save(update_fields=["description"])
        with self.assertRaises(ValidationError):
            decide_publication(
                self.problem,
                self.admin,
                publish=True,
                override_reason="Cannot override changed content",
            )
        self.problem.description = "Original\r\n"
        self.problem.save(update_fields=["description"])
        decide_publication(self.problem, self.admin, publish=True)
        self.review_run.refresh_from_db()
        self.assertEqual(self.review_run.content_hash, "a" * 64)

    def test_author_cannot_submit_non_ready_candidate(self):
        with patch(
            "judge.review.publication.workflow.publication_eligibility",
            return_value=False,
        ), self.assertRaises(ValidationError):
            self.submit()
        self.assertFalse(PublicRequest.objects.exists())

    def test_unqualified_coauthor_cannot_submit(self):
        self.author.user_permissions.clear()
        author = User.objects.get(pk=self.author.pk)
        with self.assertRaises(PermissionDenied):
            submit_publication(self.problem, author, self.review_run.pk)

    def test_withdraw_preserves_history_and_prevents_approval(self):
        row = self.submit()
        withdraw_publication(self.problem, self.author)
        row.refresh_from_db()
        self.assertEqual(row.status, PublicRequest.WITHDRAWN)
        self.assertEqual(row.review_run_id, self.review_run.pk)
        with self.assertRaises(ValidationError):
            decide_publication(self.problem, self.admin, publish=True)
        self.assertEqual(
            list(
                PublicationRequestEvent.objects.order_by("pk").values_list(
                    "status", flat=True
                )
            ),
            ["P", "W"],
        )

    def test_override_requires_reason_without_rewriting_readiness(self):
        self.submit()
        with patch(
            "judge.review.publication.workflow.publication_blockers",
            return_value=["Review requires manual assessment."],
        ):
            with self.assertRaises(ValidationError):
                decide_publication(self.problem, self.admin, publish=True)
            row = decide_publication(
                self.problem,
                self.admin,
                override_reason="Manually verified remaining concerns",
                publish=True,
            )
        self.problem.refresh_from_db()
        self.review_run.refresh_from_db()
        self.assertTrue(self.problem.is_public)
        self.assertTrue(self.problem.is_locked)
        self.assertEqual(self.review_run.readiness, "READY")
        self.assertEqual(row.override_reason, "Manually verified remaining concerns")

    def test_queue_publish_ready_candidate_needs_no_override(self):
        self.review_run.readiness = "READY_WITH_WARNINGS"
        self.review_run.save(update_fields=["readiness"])
        row = self.submit()
        response = publish_problem(
            self._request(self.admin, data={"id": self.problem.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.content)["success"])
        row.refresh_from_db()
        self.problem.refresh_from_db()
        self.assertEqual(row.override_reason, "")
        self.assertEqual(row.status, PublicRequest.APPROVED)
        self.assertTrue(self.problem.is_public)
        self.assertTrue(self.problem.is_locked)

    def test_queue_publish_requests_override_only_for_overridable_gate(self):
        row = self.submit()
        with patch(
            "judge.review.publication.workflow.publication_blockers",
            return_value=["Review requires manual assessment."],
        ):
            response = publish_problem(
                self._request(self.admin, data={"id": self.problem.pk})
            )
            self.assertEqual(response.status_code, 409)
            self.assertTrue(json.loads(response.content)["requires_override"])
            row.refresh_from_db()
            self.assertEqual(row.status, PublicRequest.PENDING)
            response = publish_problem(
                self._request(
                    self.admin,
                    data={
                        "id": self.problem.pk,
                        "override_reason": "Manually verified remaining concerns",
                    },
                )
            )
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.override_reason, "Manually verified remaining concerns")

    def test_queue_publish_stale_candidate_does_not_offer_override(self):
        row = self.submit()
        with patch(
            "judge.review.publication.workflow.current_content_hash",
            return_value="c" * 64,
        ):
            for reason in ("", "Cannot bypass stale content"):
                response = publish_problem(
                    self._request(
                        self.admin,
                        data={
                            "id": self.problem.pk,
                            "override_reason": reason,
                        },
                    )
                )
                self.assertEqual(response.status_code, 409)
                self.assertFalse(json.loads(response.content)["requires_override"])
        row.refresh_from_db()
        self.problem.refresh_from_db()
        self.assertEqual(row.status, PublicRequest.PENDING)
        self.assertFalse(self.problem.is_public)

    def test_queue_direct_publication_prompts_for_reason_and_audits_it(self):
        for reason in ("", "   "):
            response = publish_problem(
                self._request(
                    self.admin,
                    data={
                        "id": self.problem.pk,
                        "override_reason": reason,
                    },
                )
            )
            self.assertEqual(response.status_code, 409)
            self.assertTrue(json.loads(response.content)["requires_override"])
        self.assertFalse(
            PublicationRequestEvent.objects.filter(problem=self.problem).exists()
        )
        response = publish_problem(
            self._request(
                self.admin,
                data={
                    "id": self.problem.pk,
                    "override_reason": "  Reviewed manually  ",
                },
            )
        )
        self.assertEqual(response.status_code, 200)
        event = PublicationRequestEvent.objects.get(problem=self.problem)
        self.assertEqual(event.reason, "Reviewed manually")
        self.assertIsNone(event.problem_run_id)
        self.assertEqual(
            publish_problem(
                self._request(self.author, data={"id": self.problem.pk})
            ).status_code,
            403,
        )

    def test_queue_renders_override_input_only_inside_exception_modal(self):
        # Queue author links use the site's word-character username route.
        for user in (self.author, self.admin):
            user.username = user.username.replace("-", "_")
            user.save(update_fields=["username"])
        Profile.dirty_cache(self.profile.pk, self.admin_profile.pk)
        self.submit()
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("internal_problem_queue"), {"tab": "request_public"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'class="publication-override-reason"')
        self.assertNotContains(
            response, 'placeholder="Override reason (only if required)"'
        )
        self.assertContains(response, 'id="publication-override-modal"')
        self.assertContains(
            response, 'id="publication-override-reason" rows="3" required'
        )
        self.assertContains(response, "response.requires_override")

    def test_stale_candidate_cannot_be_approved_even_with_override(self):
        self.submit()
        with patch(
            "judge.review.publication.workflow.current_content_hash",
            return_value="c" * 64,
        ), self.assertRaises(ValidationError):
            decide_publication(
                self.problem, self.admin, override_reason="Override", publish=True
            )
        self.problem.refresh_from_db()
        self.assertFalse(self.problem.is_public)

    def test_lock_blocks_author_not_superuser(self):
        self.problem.is_locked = True
        self.problem.save(update_fields=["is_locked"])
        self.assertFalse(self.problem.is_editable_by(self.author))
        self.assertTrue(self.problem.is_editable_by(self.admin))
        self.assertTrue(self.problem.can_request_public_by(self.author))

    def test_locked_problem_editor_tabs_remain_readable(self):
        LanguageLimit.objects.create(
            problem=self.problem,
            language_id=self.profile.language_id,
            time_limit=2,
            memory_limit=65536,
        )
        LanguageTemplate.objects.create(
            problem=self.problem,
            language_id=self.profile.language_id,
            source="print('locked template')",
        )
        Solution.objects.create(
            problem=self.problem,
            content="Locked editorial",
            publish_on=timezone.now(),
        )
        ProblemTranslation.objects.create(
            problem=self.problem,
            language="vi",
            name="Translated title",
            description="Locked translation",
        )
        self.client.force_login(self.author)
        for public in (False, True):
            self.problem.is_public = public
            self.problem.is_locked = True
            self.problem.save(update_fields=["is_public", "is_locked"])
            for suffix in (
                "",
                "/language_limits",
                "/language_templates",
                "/solutions",
                "/translations",
            ):
                with self.subTest(public=public, suffix=suffix):
                    response = self.client.get("/problem/locktest/edit" + suffix)
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(
                        response,
                        "This content is locked. Contact a superuser to request an edit.",
                    )
                    self.assertContains(
                        response, '<fieldset class="problem-editor-readonly" disabled>'
                    )
                    self.assertContains(response, 'href="/problem/locktest/review"')
                    if not suffix:
                        self.assertTrue(
                            all(
                                field.disabled
                                for field in response.context["form"].fields.values()
                            )
                        )
                    if suffix == "/language_templates":
                        self.assertContains(response, "editor.setReadOnly(true)")

    def test_locked_editor_tabs_reject_author_writes(self):
        self.problem.is_locked = True
        self.problem.save(update_fields=["is_locked"])
        self.client.force_login(self.author)
        for suffix in (
            "",
            "/language_limits",
            "/language_templates",
            "/solutions",
            "/translations",
        ):
            with self.subTest(suffix=suffix):
                response = self.client.post(
                    "/problem/locktest/edit" + suffix,
                    self.content_payload(description="Forbidden change"),
                )
                self.assertEqual(response.status_code, 403)
        self.problem.refresh_from_db()
        self.assertEqual(self.problem.description, "Original")
        self.assertFalse(self.problem.language_limits.exists())
        self.assertFalse(self.problem.language_templates.exists())
        self.assertFalse(Solution.objects.filter(problem=self.problem).exists())
        self.assertFalse(self.problem.translations.exists())

    def test_locked_problem_review_does_not_show_content_edit_lock_notice(self):
        self.author.username = "publication_author"
        self.author.save(update_fields=["username"])
        self.problem.is_locked = True
        self.problem.save(update_fields=["is_locked"])
        self.client.force_login(self.author)
        response = self.client.get(
            reverse("problem_review_dashboard", args=[self.problem.code])
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response, "This content is locked. Contact a superuser to request an edit."
        )
        self.assertTrue(response.context["coordinator_can_run"])

    def test_locked_problem_history_does_not_show_content_edit_lock_notice(self):
        self.problem.is_locked = True
        self.problem.save(update_fields=["is_locked"])
        self.client.force_login(self.author)
        response = self.client.get(reverse("problem_log", args=[self.problem.code]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(
            response, "This content is locked. Contact a superuser to request an edit."
        )

    def test_solution_codes_uses_sidebar_instead_of_redundant_back_link(self):
        self.client.force_login(self.author)
        response = self.client.get(
            reverse("problem_solution_codes", args=[self.problem.code])
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Back to Test Data")
        self.assertContains(response, 'href="/problem/locktest/test_data"')

    def test_locked_problem_data_keeps_presentation_setup_for_authors_and_superusers(
        self,
    ):
        self.problem.is_locked = True
        self.problem.save(update_fields=["is_locked"])
        field_sets = []
        for user in (self.author, self.admin):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(
                    reverse("problem_data", args=[self.problem.code])
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(
                    response, static("widgets/problem_data_visibility.js")
                )
                self.assertContains(
                    response, "window.initProblemDataVisibility(document)"
                )
                field_sets.append(set(response.context["data_form"].fields))
                if not user.is_superuser:
                    self.assertContains(response, "<fieldset disabled>")
                    self.assertContains(
                        response,
                        "This content is locked. Contact a superuser to request an edit.",
                    )
        self.assertEqual(field_sets[0], field_sets[1])

    def test_lock_preserves_problem_detail_and_editorial_navigation(self):
        # Problem submission URLs accept word-character usernames only.
        self.author.username = "publication_author"
        self.author.save(update_fields=["username"])
        Solution.objects.create(
            problem=self.problem,
            content="Public editorial",
            is_public=True,
            publish_on=timezone.now() - timedelta(days=1),
        )
        self.client.force_login(self.author)
        for locked in (False, True):
            self.problem.is_locked = locked
            self.problem.save(update_fields=["is_locked"])
            with self.subTest(locked=locked):
                response = self.client.get(
                    reverse("problem_detail", args=[self.problem.code])
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'href="/problem/locktest/edit"')
                self.assertTrue(response.context["can_view_problem_editor"])
                self.assertEqual(response.context["can_edit_problem"], not locked)
                response = self.client.get(
                    reverse("problem_editorial", args=[self.problem.code])
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'href="/problem/locktest/edit/solutions"')

    @override_settings(MOSS_API_KEY="test-key")
    def test_lock_preserves_contest_activity_navigation_and_statistics(self):
        contest = Contest.objects.create(
            key="lockedactivitynavigation",
            name="Activity navigation",
            start_time=timezone.now() + timedelta(hours=1),
            end_time=timezone.now() + timedelta(hours=3),
            freeze_after=timedelta(minutes=30),
        )
        contest.authors.add(self.profile)
        ContestProblem.objects.create(
            contest=contest,
            problem=self.problem,
            points=1,
            order=0,
            is_result_hidden=True,
        )
        self.author.user_permissions.add(
            Permission.objects.get(codename="moss_contest")
        )
        self.client.force_login(self.author)
        for locked in (False, True):
            contest.is_locked = locked
            contest.save(update_fields=["is_locked"])
            with self.subTest(locked=locked):
                response = self.client.get(reverse("contest_edit", args=[contest.key]))
                self.assertEqual(response.status_code, 200)
                for route in (
                    "contest_stats",
                    "resolver",
                    "contest_moss",
                    "contest_final_ranking",
                ):
                    self.assertContains(
                        response, 'href="%s"' % reverse(route, args=[contest.key])
                    )
                response = self.client.get(reverse("contest_stats", args=[contest.key]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["can_edit"], not locked)
                self.assertTrue(response.context["can_manage_contest_activity"])
                context = _contest_sidebar_context(self._request(), contest)
                self.assertTrue(context["show_final_ranking"])
                self.assertTrue(context["can_manage_contest_activity"])
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get(reverse("contest_stats", args=[contest.key])).status_code,
            200,
        )
        outsider = User.objects.create_user("activity-outsider")
        Profile.objects.get_or_create(
            user=outsider, defaults={"language_id": self.profile.language_id}
        )
        self.client.force_login(outsider)
        # ContestMixin renders its existing generic not-found page for hidden contests.
        response = self.client.get(reverse("contest_stats", args=[contest.key]))
        self.assertContains(response, "No such contest")
        self.assertNotContains(response, "Activity navigation Statistics")
        self.assertEqual(
            self.client.get(reverse("resolver", args=[contest.key])).status_code, 403
        )

    def test_locked_editor_read_access_does_not_bypass_team_permissions(self):
        outsider = User.objects.create_user("lock-outsider")
        Profile.objects.get_or_create(
            user=outsider, defaults={"language_id": self.profile.language_id}
        )
        curator = User.objects.create_user("lock-curator")
        curator_profile, _ = Profile.objects.get_or_create(
            user=curator,
            defaults={"language_id": self.profile.language_id},
        )
        self.problem.curators.add(curator_profile)
        self.problem.is_public = True
        self.problem.save(update_fields=["is_public"])
        for suffix in (
            "",
            "/language_limits",
            "/language_templates",
            "/solutions",
            "/translations",
        ):
            path = "/problem/locktest/edit" + suffix
            with self.subTest(suffix=suffix):
                self.client.logout()
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response.url)
                self.client.force_login(outsider)
                self.assertEqual(self.client.get(path).status_code, 403)
                self.client.force_login(curator)
                self.assertEqual(self.client.get(path).status_code, 200)
                self.client.force_login(self.admin)
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(
                    response, '<fieldset class="problem-editor-readonly" disabled>'
                )

    def test_editor_views_enforce_lock_without_mutation_middleware(self):
        self.problem.is_locked = True
        self.problem.save(update_fields=["is_locked"])
        for view in (
            ProblemEdit,
            ProblemEditLanguageLimits,
            ProblemEditLanguageTemplates,
            ProblemEditSolutions,
            ProblemEditTranslations,
        ):
            with self.subTest(view=view.__name__):
                request = RequestFactory().post("/problem/locktest/edit", {})
                request.user = self.author
                request.profile = self.profile
                with self.assertRaises(PermissionDenied):
                    view.as_view()(request, problem=self.problem.code)

    def test_lock_does_not_prevent_request_withdrawal(self):
        row = self.submit()
        self.problem.is_locked = True
        self.problem.save(update_fields=["is_locked"])
        withdraw_publication(self.problem, self.author)
        row.refresh_from_db()
        self.assertEqual(row.status, PublicRequest.WITHDRAWN)

    def test_problem_pending_ui_replaces_request_with_withdraw_on_review_and_content(
        self,
    ):
        self.author.username = "publication_author"
        self.author.save(update_fields=["username"])
        row = self.submit()
        self.problem.is_locked = True
        self.problem.save(update_fields=["is_locked"])
        self.client.force_login(self.author)
        original_credit = self.review_run.credit_consumed
        for route in ("problem_edit", "problem_review_dashboard"):
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=[self.problem.code]))
                self.assertEqual(response.status_code, 200)
                soup = BeautifulSoup(response.content, "html.parser")
                labels = [
                    button.get_text(strip=True) for button in soup.select("button")
                ]
                self.assertIn("Withdraw request", labels)
                self.assertNotIn("Request publication", labels)
        response = self.client.post(
            reverse("internal_cancel_request_public"), {"id": self.problem.pk}
        )
        self.assertTrue(response.json()["success"])
        row.refresh_from_db()
        self.review_run.refresh_from_db()
        self.assertEqual(row.status, PublicRequest.WITHDRAWN)
        self.assertEqual(self.review_run.credit_consumed, original_credit)
        self.assertEqual(self.review_run.readiness, "READY")
        for route in ("problem_edit", "problem_review_dashboard"):
            response = self.client.get(reverse(route, args=[self.problem.code]))
            labels = [
                button.get_text(strip=True)
                for button in BeautifulSoup(response.content, "html.parser").select(
                    "button"
                )
            ]
            self.assertIn("Request publication", labels)
            self.assertNotIn("Withdraw request", labels)

    def test_publishing_partial_save_locks_atomically_and_unlock_stays(self):
        self.problem.is_public = True
        self.problem.save(update_fields=["is_public"])
        self.problem.refresh_from_db()
        self.assertTrue(self.problem.is_locked)
        self.problem.is_locked = False
        self.problem.save(update_fields=["is_locked"])
        self.problem.name = "Edited after explicit unlock"
        self.problem.save()
        self.problem.refresh_from_db()
        self.assertFalse(self.problem.is_locked)

    def test_toggle_lock_is_superuser_only(self):
        request = RequestFactory().post("/lock", {"locked": "1"})
        request.user = self.author
        self.assertEqual(
            toggle_content_lock(request, "problem", self.problem.pk).status_code, 403
        )
        request.user = self.admin
        self.assertEqual(
            toggle_content_lock(request, "problem", self.problem.pk).status_code, 200
        )
        self.problem.refresh_from_db()
        self.assertTrue(self.problem.is_locked)

    def _mutate(
        self, *, changed=False, confirmed=False, path=None, ajax=True, fields=None
    ):
        pending = PublicRequest.objects.filter(
            problem=self.problem, status=PublicRequest.PENDING
        ).first()
        data = (
            {
                "confirm_withdraw_publication": "1",
                "publication_confirmation_token": publication_confirmation_token(
                    pending
                ),
            }
            if confirmed
            else {}
        )
        data.update(fields or {})
        request = RequestFactory().post(
            path or "/problem/locktest/edit",
            data,
            **({"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"} if ajax else {}),
        )
        request.user = self.author
        request.profile = self.profile
        request.session = {}
        request.LANGUAGE_CODE = "en"

        def view(_request):
            obj = Problem.objects.get(pk=self.problem.pk)
            if changed:
                obj.description = "Changed"
            obj.save(update_fields=["description"])
            return JsonResponse({"success": True})

        with patch(
            "judge.content_mutation.workflow.current_content_hash",
            side_effect=lambda obj: obj.description,
        ), patch("judge.content_mutation.workflow.invalidate_readiness"):
            return ContentMutationMiddleware(view)(request)

    def test_no_op_save_preserves_pending_request(self):
        row = self.submit()
        self.assertEqual(self._mutate().status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.status, PublicRequest.PENDING)

    def test_changed_unconfirmed_save_rolls_back(self):
        row = self.submit()
        self.assertEqual(self._mutate(changed=True).status_code, 409)
        self.problem.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(self.problem.description, "Original")
        self.assertEqual(row.status, PublicRequest.PENDING)

    def test_changed_confirmed_save_withdraws(self):
        row = self.submit()
        self.assertEqual(self._mutate(changed=True, confirmed=True).status_code, 200)
        self.problem.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(self.problem.description, "Changed")
        self.assertEqual(row.status, PublicRequest.WITHDRAWN)

    def test_browser_save_shows_html_and_preserves_submitted_fields(self):
        row = self.submit()
        description = 'Draft\n<script>alert("x")</script> & math $a_i$'
        response = self._mutate(
            changed=True,
            ajax=False,
            fields={"description": description, "allowed_languages": ["PY3", "CPP17"]},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("text/html", response["Content-Type"])
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertContains(
            response, "Save changes and withdraw request", status_code=409
        )
        self.assertNotContains(response, '<script>alert("x")</script>', status_code=409)
        inputs = ConfirmationInputs(response.content.decode()).fields
        self.assertIn(("description", description), inputs)
        self.assertEqual(
            [value for name, value in inputs if name == "allowed_languages"],
            ["PY3", "CPP17"],
        )
        self.assertIn(("confirm_withdraw_publication", "1"), inputs)
        self.assertIn(
            ("publication_confirmation_token", publication_confirmation_token(row)),
            inputs,
        )
        self.problem.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(self.problem.description, "Original")
        self.assertEqual(row.status, PublicRequest.PENDING)

    def test_browser_confirmed_save_withdraws_request(self):
        row = self.submit()
        response = self._mutate(changed=True, confirmed=True, ajax=False)
        self.assertEqual(response.status_code, 200)
        self.problem.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(self.problem.description, "Changed")
        self.assertEqual(row.status, PublicRequest.WITHDRAWN)

    def test_json_accept_header_keeps_machine_readable_confirmation(self):
        row = self.submit()
        request = self._request(path="/problem/locktest/test_data/upload")
        request.META["HTTP_ACCEPT"] = "application/json"
        response = ContentMutationMiddleware(
            lambda request: self.fail("Upload must wait for confirmation")
        )(request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(
            (
                response.json()
                if hasattr(response, "json")
                else json.loads(response.content)
            ),
            {
                "success": False,
                "requires_publication_confirmation": True,
                "publication_request_id": row.pk,
                "publication_confirmation_token": publication_confirmation_token(row),
                "error": "Saving changes will withdraw the pending publication request. Confirm to save the changed draft.",
            },
        )

    def test_browser_upload_requires_files_to_be_selected_again(self):
        self.submit()
        request = self._request(
            path="/problem/locktest/test_data/upload",
            data={"file": StringIO("uncommitted file")},
        )
        response = ContentMutationMiddleware(
            lambda request: self.fail("Upload must wait for confirmation")
        )(request)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'type="file" name="file"', status_code=409)
        self.assertContains(
            response, "Please select the attached files again", status_code=409
        )

    def test_file_mutation_requires_confirmation_before_execution(self):
        self.submit()
        request = RequestFactory().post(
            "/problem/locktest/test_data/upload",
            {},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        request.user, request.profile = self.author, self.profile
        with patch("judge.content_mutation.workflow.current_content_hash"), patch(
            "judge.content_mutation.workflow.invalidate_readiness"
        ):
            view = lambda _request: self.fail(
                "Must not perform irreversible upload before confirmation"
            )
            self.assertEqual(ContentMutationMiddleware(view)(request).status_code, 409)

    def test_outsider_upload_does_not_disclose_publication_identity(self):
        self.submit()
        outsider = User.objects.create_user("publication-upload-outsider")
        profile, _ = Profile.objects.get_or_create(
            user=outsider, defaults={"language": self.profile.language}
        )
        request = self._request(
            path="/problem/locktest/test_data/upload", user=outsider
        )
        request.profile = profile
        response = ContentMutationMiddleware(
            lambda request: JsonResponse({"error": "Permission denied"}, status=404)
        )(request)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("publication_confirmation_token", json.loads(response.content))

    def test_stale_save_consent_cannot_withdraw_newly_submitted_candidate(self):
        row = self.submit()
        old_token = publication_confirmation_token(row)
        withdraw_publication(self.problem, self.author)
        new_run = ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            workflow_version=2,
            content_hash="a" * 64,
            status=ProblemReviewRun.DONE,
            readiness="READY",
        )
        row = submit_publication(self.problem, self.author, new_run.pk)
        for path, ajax in (
            ("/problem/locktest/edit", True),
            ("/problem/locktest/edit", False),
            ("/problem/locktest/test_data/upload", True),
            ("/problem/locktest/test_data/upload", False),
        ):
            request = self._request(
                path=path,
                data={
                    "confirm_withdraw_publication": "1",
                    "publication_confirmation_token": old_token,
                },
            )
            if ajax:
                request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"

            def view(_request):
                Problem.objects.filter(pk=self.problem.pk).update(
                    description="Unconfirmed change"
                )
                return JsonResponse({"success": True})

            with patch(
                "judge.content_mutation.workflow.current_content_hash",
                side_effect=lambda obj: obj.description,
            ):
                response = ContentMutationMiddleware(view)(request)
            self.assertEqual(response.status_code, 409)
            if ajax:
                self.assertEqual(
                    json.loads(response.content)["publication_confirmation_token"],
                    publication_confirmation_token(row),
                )
            else:
                tokens = [
                    value
                    for name, value in ConfirmationInputs(
                        response.content.decode()
                    ).fields
                    if name == "publication_confirmation_token"
                ]
                self.assertEqual(tokens, [publication_confirmation_token(row)])
            self.problem.refresh_from_db()
            row.refresh_from_db()
            self.assertEqual(self.problem.description, "Original")
            self.assertEqual(row.status, PublicRequest.PENDING)

    def test_locked_quiz_retains_grading_authority(self):
        quiz = Quiz.objects.create(code="lockedquiz", title="Quiz", is_locked=True)
        quiz.authors.add(self.profile)
        self.assertFalse(quiz.is_editable_by(self.author))
        self.assertTrue(quiz.can_manage_attempts_by(self.author))

    def test_locked_contest_retains_coordinator_activity_but_not_edit_authority(self):
        contest = Contest.objects.create(
            key="activity-lock",
            name="Activity",
            is_locked=True,
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
        )
        contest.authors.add(self.profile)
        request = self._request()
        self.assertFalse(contest.is_editable_by(self.author))
        self.assertTrue(contest.can_manage_activity_by(self.author))
        self.assertTrue(can_view_contest_clarifications(contest, request))
        quiz = Quiz.objects.create(code="activityquiz", title="Activity quiz")
        item = ContestProblem.objects.create(
            contest=contest, quiz=quiz, is_result_hidden=True, points=1, order=0
        )
        self.assertFalse(quiz.should_hide_result(self.author, contest_problem=item))

    def test_locked_coordinator_retains_api_results_resolver_and_submission_read(self):
        contest = Contest.objects.create(
            key="locked-coordinator-api",
            name="Locked API",
            is_locked=True,
            start_time=timezone.now() + timedelta(hours=1),
            end_time=timezone.now() + timedelta(hours=3),
            freeze_after=timedelta(minutes=30),
        )
        contest.authors.add(self.profile)
        ContestProblem.objects.create(
            contest=contest,
            problem=self.problem,
            points=1,
            order=0,
            is_result_hidden=True,
        )
        request = RequestFactory().get("/api/contest")
        request.user, request.profile = self.author, self.profile
        response = api_v1_contest_detail(request, contest.key)
        self.assertEqual(
            json.loads(response.content)["problems"][0]["code"], self.problem.code
        )
        self.assertEqual(hidden_result_problem_ids(contest, self.author), set())
        request = RequestFactory().get("/resolver", {"json": "1"})
        request.user, request.profile = self.author, self.profile
        with patch.object(Resolver, "get_contest_json", return_value={"ok": True}):
            self.assertEqual(
                Resolver.as_view()(request, contest=contest.key).status_code, 200
            )
        submission = Submission(
            user=self.admin_profile,
            problem=self.problem,
            language=self.profile.language,
        )
        with patch.object(
            Submission,
            "contest_object",
            new_callable=PropertyMock,
            return_value=contest,
        ):
            self.assertTrue(submission.is_accessible_by(self.profile))

    def test_locked_problem_preparation_reads_and_evidence_runs_remain_accessible(self):
        self.problem.is_locked = True
        self.problem.save(update_fields=["is_locked"])
        self.assertTrue(self.problem.is_subs_manageable_by(self.author))
        self.assertTrue(self.problem.can_request_public_by(self.author))
        self.assertFalse(self.problem.is_editable_by(self.author))
        for method, route, allowed in (
            ("GET", "problem_data", True),
            ("POST", "problem_validate_testcases", True),
            ("POST", "problem_solution_codes_run", True),
            ("POST", "problem_solution_codes_save", False),
        ):
            request = getattr(RequestFactory(), method.lower())(
                "/problem/locktest/test_data"
            )
            request.user, request.profile = self.author, self.profile
            request.resolver_match = SimpleNamespace(url_name=route)
            view = ProblemManagerMixin()
            view.setup(request, problem=self.problem.code)
            if allowed:
                self.assertEqual(view.get_object().pk, self.problem.pk)
            else:
                with self.assertRaises(Http404):
                    view.get_object()

    def test_admin_revision_restore_is_a_content_mutation(self):
        request = self._request()
        match = SimpleNamespace(
            app_name="admin",
            url_name="judge_problem_revision",
            kwargs={"object_id": str(self.problem.pk), "version_id": "123"},
        )
        self.assertEqual(
            mutation_target(request, match), (Problem, {"pk": str(self.problem.pk)})
        )

    def test_history_cannot_be_rewritten(self):
        self.submit()
        event = PublicationRequestEvent.objects.get()
        event.reason = "Changed historical record"
        with self.assertRaises(ValidationError):
            event.save()

    def test_candidate_hash_must_match_bound_run_even_with_override(self):
        row = self.submit()
        row.candidate_hash = "c" * 64
        row.save()
        with patch(
            "judge.review.publication.workflow.current_content_hash",
            return_value="c" * 64,
        ), self.assertRaises(ValidationError):
            decide_publication(
                self.problem, self.admin, override_reason="Reason", publish=True
            )

    def _request(self, user=None, path="/admin/", data=None):
        request = RequestFactory().post(path, data or {})
        request.user = user or self.author
        request.profile = request.user.profile
        request.session = {}
        request.LANGUAGE_CODE = "en"
        return request

    def test_admin_global_edit_permission_does_not_bypass_lock(self):
        self.author.user_permissions.add(
            Permission.objects.get(codename="edit_all_problem")
        )
        user = User.objects.get(pk=self.author.pk)
        self.problem.is_locked = True
        self.problem.save(update_fields=["is_locked"])
        self.assertFalse(
            ProblemAdmin(Problem, admin.site).has_change_permission(
                self._request(user), self.problem
            )
        )
        quiz = Quiz.objects.create(code="adminlockedquiz", title="Quiz", is_locked=True)
        self.assertFalse(
            QuizAdmin(Quiz, admin.site).has_change_permission(self._request(user), quiz)
        )

    def test_admin_post_global_edit_permission_does_not_bypass_lock(self):
        self.author.user_permissions.add(
            Permission.objects.get(codename="edit_all_post")
        )
        user = User.objects.get(pk=self.author.pk)
        post = BlogPost.objects.create(
            title="Post",
            slug="post",
            content="Text",
            publish_on=timezone.now(),
            is_locked=True,
        )
        self.assertFalse(
            BlogPostAdmin(BlogPost, admin.site).has_change_permission(
                self._request(user), post
            )
        )

    def test_admin_bulk_locked_selection_is_rejected(self):
        self.problem.is_locked = True
        self.problem.save(update_fields=["is_locked"])
        request = self._request(
            path="/admin/judge/problem/",
            data={"action": "delete_selected", "_selected_action": [self.problem.pk]},
        )
        view = lambda _request: self.fail("Locked bulk action must not execute")
        self.assertEqual(ContentMutationMiddleware(view)(request).status_code, 403)

    def test_admin_single_object_lock_is_rechecked_before_view(self):
        self.problem.is_locked = True
        self.problem.save(update_fields=["is_locked"])
        request = self._request(
            path="/admin/judge/problem/%s/change/" % self.problem.pk
        )
        view = lambda _request: self.fail("Locked admin edit must not execute")
        self.assertEqual(ContentMutationMiddleware(view)(request).status_code, 403)

    def test_publication_admin_controls_are_disabled_under_new_policy(self):
        request = self._request(self.admin)
        problem_admin = ProblemAdmin(Problem, admin.site)
        self.assertIn(
            "is_public", problem_admin.get_readonly_fields(request, self.problem)
        )
        self.assertNotIn("make_public", problem_admin.get_actions(request))
        contest_admin = ContestAdmin(Contest, admin.site)
        self.assertNotIn("is_visible", contest_admin.get_readonly_fields(request))
        self.assertNotIn("make_visible", contest_admin.get_actions(request))
        request_admin = ContestPublicRequestAdmin(ContestPublicRequest, admin.site)
        self.assertFalse(request_admin.has_change_permission(request))
        self.assertFalse(request_admin.has_add_permission(request))

    def test_object_local_contest_lock_does_not_lock_private_problem(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key="localonlylock",
            name="Locked contest",
            start_time=now,
            end_time=now + timezone.timedelta(hours=1),
            is_visible=True,
        )
        ContestProblem.objects.create(
            contest=contest, problem=self.problem, order=1, points=100
        )
        self.problem.refresh_from_db()
        self.assertTrue(contest.is_locked)
        self.assertFalse(self.problem.is_locked)
        self.assertTrue(self.problem.is_editable_by(self.author))

    def test_locked_direct_upload_token_cannot_mutate_content(self):
        self.problem.is_locked = True
        self.problem.save(update_fields=["is_locked"])
        request = self._request(
            path="/api/upload/save/", data={"upload_token": "old-token"}
        )
        token = {
            "profile_id": self.profile.pk,
            "model_name": "judge.Problem",
            "object_id": self.problem.pk,
        }
        with patch("judge.content_mutation.get_upload_token_data", return_value=token):
            view = lambda _request: self.fail("Old token must not bypass current Lock")
            self.assertEqual(ContentMutationMiddleware(view)(request).status_code, 403)

    def test_removed_editor_direct_upload_token_is_rejected(self):
        self.problem.authors.clear()
        request = self._request(
            path="/api/upload/save/", data={"upload_token": "old-token"}
        )
        token = {
            "profile_id": self.profile.pk,
            "model_name": "judge.Problem",
            "object_id": self.problem.pk,
        }
        with patch("judge.content_mutation.get_upload_token_data", return_value=token):
            view = lambda _request: self.fail(
                "Old token must not bypass current edit permissions"
            )
            self.assertEqual(ContentMutationMiddleware(view)(request).status_code, 403)

    def test_json_list_solution_save_supports_confirmation_header(self):
        row = self.submit()
        request = RequestFactory().post(
            "/problem/locktest/test_data/solution_codes/save",
            data="[]",
            content_type="application/json",
            HTTP_X_CONFIRM_WITHDRAW_PUBLICATION="1",
            HTTP_X_PUBLICATION_CONFIRMATION_TOKEN=publication_confirmation_token(row),
        )
        request.user, request.profile = self.author, self.profile

        def view(_request):
            Problem.objects.filter(pk=self.problem.pk).update(description="Changed")
            return JsonResponse({"success": True})

        with patch(
            "judge.content_mutation.workflow.current_content_hash",
            side_effect=lambda obj: obj.description,
        ), patch("judge.content_mutation.workflow.invalidate_readiness"):
            self.assertEqual(ContentMutationMiddleware(view)(request).status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.status, PublicRequest.WITHDRAWN)

    def test_question_lock_is_local_and_enforced(self):
        question = QuizQuestion.objects.create(
            title="Question", content="Text", question_type="MC"
        )
        question.authors.add(self.profile)
        self.assertTrue(question.is_editable_by(self.author))
        question.is_public = True
        question.save(update_fields=["is_public"])
        question.refresh_from_db()
        self.assertTrue(question.is_locked)
        self.assertFalse(question.is_editable_by(self.author))
        request = self._request(path="/quiz/questions/%s/edit/" % question.pk)
        self.assertEqual(
            ContentMutationMiddleware(
                lambda request: self.fail("Locked question edited")
            )(request).status_code,
            403,
        )
        self.assertFalse(
            QuizQuestionAdmin(QuizQuestion, admin.site).has_delete_permission(
                request, question
            )
        )

    def test_newer_same_candidate_review_requires_explicit_approval_override(self):
        self.submit()
        ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            workflow_version=2,
            content_hash=self.review_run.content_hash,
            status=ProblemReviewRun.DONE,
            readiness="NEEDS_AUTHOR_FIX",
        )
        with self.assertRaises(ValidationError):
            decide_publication(self.problem, self.admin)
        row = decide_publication(
            self.problem,
            self.admin,
            override_reason="Reviewed later disagreement manually",
        )
        self.assertEqual(row.review_run_id, self.review_run.pk)

    def test_historical_contest_metadata_change_preserves_publication_binding(self):
        contest = Contest.objects.create(
            key="hashpolicymetadata",
            name="Original display title",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=3),
        )
        contest.authors.add(self.profile)
        snapshot = build_contest_snapshot(contest, [])
        run = ContestReviewRun.objects.create(
            contest=contest,
            triggered_by=self.profile,
            workflow_version=2,
            snapshot=snapshot,
            content_hash="old immutable broad hash",
            status=ContestReviewRun.DONE,
            readiness="READY",
        )
        with patch(
            "judge.review.publication.workflow.current_content_hash",
            return_value=snapshot["content_hash"],
        ):
            request_row = submit_publication(contest, self.author, run.pk)
            contest.name = "Updated display title"
            contest.save(update_fields=["name"])
            contest.testers.add(self.admin_profile)
            approved = decide_publication(contest, self.admin)
        self.assertEqual(approved.status, ContestPublicRequest.APPROVED)
        self.assertEqual(approved.candidate_hash, "old immutable broad hash")
        self.assertEqual(approved.pk, request_row.pk)
        run.refresh_from_db()
        self.assertEqual(run.snapshot, snapshot)
        self.assertEqual(run.content_hash, "old immutable broad hash")

    def test_newer_equivalent_contest_review_across_hash_versions_blocks_approval(self):
        contest = Contest.objects.create(
            key="hashpolicyconflict",
            name="Original display title",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=3),
        )
        contest.authors.add(self.profile)
        snapshot = build_contest_snapshot(contest, [])
        run = ContestReviewRun.objects.create(
            contest=contest,
            triggered_by=self.profile,
            workflow_version=2,
            snapshot=snapshot,
            content_hash="old immutable broad hash",
            status=ContestReviewRun.DONE,
            readiness="READY",
        )
        ContestReviewRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - timedelta(days=1)
        )
        with patch(
            "judge.review.publication.workflow.current_content_hash",
            return_value=snapshot["content_hash"],
        ):
            submit_publication(contest, self.author, run.pk)
            contest.name = "Updated display title"
            contest.save(update_fields=["name"])
            newer_snapshot = build_contest_snapshot(contest, [])
            self.assertEqual(newer_snapshot["content_hash"], snapshot["content_hash"])
            ContestReviewRun.objects.create(
                contest=contest,
                triggered_by=self.profile,
                workflow_version=2,
                snapshot=newer_snapshot,
                content_hash=newer_snapshot["content_hash"],
                status=ContestReviewRun.DONE,
                readiness="NEEDS_AUTHOR_FIX",
            )
            with self.assertRaises(ValidationError):
                decide_publication(contest, self.admin)

    def test_private_problem_outsider_submission_does_not_block_publication(self):
        self.submit()
        outsider = User.objects.create_user("publication-outsider")
        profile, _ = Profile.objects.get_or_create(
            user=outsider, defaults={"language": self.profile.language}
        )
        Submission.objects.create(
            user=profile,
            problem=self.problem,
            language=self.profile.language,
            status="D",
            result="AC",
        )
        with self.assertNumQueries(0):
            self.assertFalse(publication_exposure_concern(self.problem))
        self.submit()
        row = decide_publication(self.problem, self.admin)
        self.assertEqual(row.status, PublicRequest.APPROVED)

    def test_problem_review_and_content_do_not_show_exposure_checks(self):
        self.ready_patch.stop()
        self.blockers_patch.stop()
        self.author.username = "publication_author"
        self.author.save(update_fields=["username"])
        Profile.dirty_cache(self.profile.pk)
        outsider = User.objects.create_user("live-blocker-outsider")
        profile, _ = Profile.objects.get_or_create(
            user=outsider, defaults={"language": self.profile.language}
        )
        Submission.objects.create(
            user=profile,
            problem=self.problem,
            language=self.profile.language,
            status="D",
            result="AC",
        )
        self.client.force_login(self.author)
        for path in (
            "/problem/%s/review" % self.problem.code,
            "/problem/%s/edit" % self.problem.code,
        ):
            with self.subTest(path=path), patch(
                "judge.views.coordinator_review.current_content_hash",
                return_value="a" * 64,
            ):
                response = self.client.get(path)
                self.assertNotContains(response, "Possible pre-release access")
                self.assertNotContains(response, "Publication access check")
                self.assertContains(response, 'id="publication-request-hint"')
                self.assertNotContains(response, "Publication needs attention")
        self.review_run.refresh_from_db()
        self.assertEqual(self.review_run.readiness, "READY")
        self.assertFalse(self.review_run.current_stale)

    def test_contest_checks_outside_submissions_against_its_current_team(self):
        contest = Contest.objects.create(
            key="exposure-team",
            name="Contest access check",
            start_time=timezone.now() + timedelta(days=1),
            end_time=timezone.now() + timedelta(days=2),
        )
        ContestProblem.objects.create(
            contest=contest, problem=self.problem, order=0, points=100
        )
        contest.authors.add(self.profile)
        outsider = User.objects.create_user("contest-exposure-outsider")
        profile, _ = Profile.objects.get_or_create(
            user=outsider, defaults={"language": self.profile.language}
        )
        # Problem authorship does not automatically grant contest-team trust.
        self.problem.authors.add(profile)
        submission = Submission.objects.create(
            user=profile,
            problem=self.problem,
            language=self.profile.language,
            status="D",
            result="AC",
        )
        self.assertFalse(publication_exposure_concern(self.problem))
        self.assertTrue(publication_exposure_concern(contest))
        self.assertIsNone(publication_access_details(self.problem))
        details = publication_access_details(contest)
        self.assertEqual(len(details["groups"]), 1)
        self.assertEqual(details["submission_count"], 1)
        group = details["groups"][0]
        activity = group["records"][0]
        self.assertEqual(group["profile"].pk, profile.pk)
        self.assertEqual(group["item_url"], "/problem/locktest")
        self.assertEqual(activity["date"], submission.date)
        self.assertEqual(activity["context"], "Outside this contest")
        self.assertEqual(group["access_labels"], ["Problem author"])
        for role in ("authors", "curators", "testers"):
            with self.subTest(role=role):
                relation = getattr(contest, role)
                relation.add(profile)
                self.assertFalse(publication_exposure_concern(contest))
                self.assertEqual(
                    publication_access_details(contest), {"material_concern": False}
                )
                relation.remove(profile)
                self.assertTrue(publication_exposure_concern(contest))

    def test_contest_exposure_excludes_superusers_and_public_participation(self):
        started_at = timezone.now() - timedelta(hours=1)
        contest = Contest.objects.create(
            key="exposure-released",
            name="Released contest",
            is_visible=True,
            start_time=started_at,
            end_time=timezone.now() + timedelta(hours=1),
        )
        ContestProblem.objects.create(
            contest=contest, problem=self.problem, order=0, points=100
        )
        contest.authors.add(self.profile)
        Submission.objects.create(
            user=self.admin_profile,
            problem=self.problem,
            language=self.profile.language,
            status="D",
            result="AC",
        )
        self.assertFalse(publication_exposure_concern(contest))
        outsider = User.objects.create_user("contest-public-participant")
        profile, _ = Profile.objects.get_or_create(
            user=outsider, defaults={"language": self.profile.language}
        )
        submission = Submission.objects.create(
            user=profile,
            problem=self.problem,
            language=self.profile.language,
            contest_object=contest,
            status="D",
            result="AC",
        )
        self.assertFalse(publication_exposure_concern(contest))
        Submission.objects.filter(pk=submission.pk).update(
            date=started_at - timedelta(minutes=1)
        )
        self.assertTrue(publication_exposure_concern(contest))

    def test_contest_access_details_are_bounded_and_absent_from_ai_snapshots(self):
        contest = Contest.objects.create(
            key="exposure-bounded",
            name="Bounded access evidence",
            start_time=timezone.now() + timedelta(days=1),
            end_time=timezone.now() + timedelta(days=2),
        )
        ContestProblem.objects.create(
            contest=contest, problem=self.problem, order=0, points=100
        )
        contest.authors.add(self.profile)
        outsider = User.objects.create_user("bounded-outsider")
        profile, _ = Profile.objects.get_or_create(
            user=outsider, defaults={"language": self.profile.language}
        )
        Submission.objects.bulk_create(
            [
                Submission(
                    user=profile,
                    problem=self.problem,
                    language=self.profile.language,
                    status="D",
                    result="AC",
                )
                for _ in range(4)
            ]
        )
        details = publication_access_details(contest, limit=2)
        self.assertEqual(details["submission_count"], 4)
        self.assertEqual(len(details["groups"]), 1)
        self.assertEqual(details["groups"][0]["shown_records"], 2)
        self.assertEqual(details["groups"][0]["record_count"], 4)
        # More accounts must not vanish behind another account's sample limit.
        for index in range(6):
            user = User.objects.create_user("group_outsider_%s" % index)
            member, _ = Profile.objects.get_or_create(
                user=user, defaults={"language": self.profile.language}
            )
            Submission.objects.create(
                user=member,
                problem=self.problem,
                language=self.profile.language,
                status="D",
                result="AC",
            )
        details = publication_access_details(contest, limit=2)
        self.assertEqual(details["submission_count"], 10)
        self.assertEqual(len(details["groups"]), 7)
        self.assertEqual(len(details["visible_groups"]), 5)
        self.assertEqual(len(details["more_groups"]), 2)
        self.assertIn("7 accounts", details["summary"])
        self.assertIn("10 submissions", details["summary"])
        self.problem.testers.add(profile)
        details = publication_access_details(contest, limit=2)
        mixed = next(
            group for group in details["groups"] if group["profile"].pk == profile.pk
        )
        self.assertEqual(mixed["access_labels"], ["Problem tester"])
        Submission.objects.filter(user=profile).delete()
        details = publication_access_details(contest, limit=2)
        permission_only = next(
            group for group in details["groups"] if group["profile"].pk == profile.pk
        )
        self.assertEqual(permission_only["access_labels"], ["Problem tester"])
        self.assertEqual(permission_only["record_count"], 0)
        self.assertEqual(permission_only["records"], [])
        frozen = build_contest_snapshot(contest, [])["exposure"]
        self.assertTrue(frozen["material_concern"])
        self.assertNotIn("submission_details", frozen)
        self.assertNotIn("quiz_attempt_details", frozen)

    def test_contest_access_details_include_quiz_attempts_without_sources(self):
        contest = Contest.objects.create(
            key="exposure-quiz",
            name="Quiz access evidence",
            start_time=timezone.now() + timedelta(days=1),
            end_time=timezone.now() + timedelta(days=2),
        )
        contest.authors.add(self.profile)
        quiz = Quiz.objects.create(code="exposurequiz", title="Private quiz")
        ContestProblem.objects.create(contest=contest, quiz=quiz, order=0, points=100)
        outsider = User.objects.create_user("quiz-outsider")
        profile, _ = Profile.objects.get_or_create(
            user=outsider, defaults={"language": self.profile.language}
        )
        attempt = QuizAttempt.objects.create(user=profile, quiz=quiz)
        details = publication_access_details(contest)
        self.assertEqual(details["attempt_count"], 1)
        group = details["groups"][0]
        row = group["records"][0]
        self.assertEqual(group["profile"].pk, profile.pk)
        self.assertEqual(row["activity"], "Quiz attempt #%s" % attempt.pk)
        self.assertEqual(group["item_name"], quiz.title)
        self.assertEqual(group["attempt_label"], "1 quiz attempt")
        self.assertEqual(row["date"], attempt.start_time)
        self.assertNotIn("source", row)
        self.assertTrue(contest_publication_exposure(contest)["material_concern"])
        quiz.testers.add(profile)
        self.assertEqual(
            publication_access_details(contest)["groups"][0]["access_labels"],
            ["Quiz tester"],
        )
        attempt.delete()
        permission_only = publication_access_details(contest)["groups"][0]
        self.assertEqual(permission_only["access_labels"], ["Quiz tester"])
        self.assertEqual(permission_only["record_count"], 0)

    def test_pending_request_explains_newer_review_before_approval(self):
        self.author.username = "publication_author"
        self.author.save(update_fields=["username"])
        Profile.dirty_cache(self.profile.pk)
        row = self.submit()
        newer = ProblemReviewRun.objects.create(
            problem=self.problem,
            triggered_by=self.profile,
            workflow_version=2,
            content_hash=self.review_run.content_hash,
            status=ProblemReviewRun.DONE,
            readiness="NEEDS_AUTHOR_FIX",
        )
        self.client.force_login(self.admin)
        for path in (
            "/problem/%s/review" % self.problem.code,
            "/problem/%s/edit" % self.problem.code,
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertContains(response, "Withdraw request")
        with self.assertRaisesMessage(
            ValidationError, "A newer review of the same content exists."
        ):
            decide_publication(self.problem, self.admin)
        row.refresh_from_db()
        self.assertEqual(row.review_run_id, self.review_run.pk)
        self.assertNotEqual(row.review_run_id, newer.pk)
        self.assertEqual(row.status, PublicRequest.PENDING)

    def test_public_and_released_contest_submissions_are_not_private_leaks(self):
        outsider = User.objects.create_user("publication-public-outsider")
        profile, _ = Profile.objects.get_or_create(
            user=outsider, defaults={"language": self.profile.language}
        )
        contest = Contest.objects.create(
            key="released-locktest",
            name="Released",
            is_visible=True,
            start_time=timezone.now() - timedelta(hours=1),
            end_time=timezone.now() + timedelta(hours=1),
        )
        Submission.objects.create(
            user=profile,
            problem=self.problem,
            language=self.profile.language,
            contest_object=contest,
            status="D",
            result="AC",
        )
        self.assertFalse(publication_exposure_concern(self.problem))
        contest.is_private = True
        contest.save(update_fields=["is_private"])
        self.assertFalse(publication_exposure_concern(self.problem))
        Problem.objects.filter(pk=self.problem.pk).update(is_public=True)
        self.problem.refresh_from_db()
        Submission.objects.create(
            user=profile,
            problem=self.problem,
            language=self.profile.language,
            status="D",
            result="AC",
        )
        self.assertFalse(publication_exposure_concern(self.problem))

    @override_settings(AUTO_REVIEW_COORDINATOR_ENABLED=False)
    def test_disabled_rollout_does_not_auto_lock_but_explicit_lock_is_enforced(self):
        quiz = Quiz.objects.create(
            code="legacy-unlocked", title="Legacy", is_public=True
        )
        quiz.authors.add(self.profile)
        self.assertFalse(quiz.is_locked)
        self.assertTrue(quiz.is_editable_by(self.author))
        quiz.is_locked = True
        quiz.save(update_fields=["is_locked"])
        self.assertFalse(quiz.is_editable_by(self.author))

    @override_settings(AUTO_REVIEW_COORDINATOR_ENABLED=False)
    def test_legacy_bulk_publication_does_not_silently_enable_lock(self):
        problem_admin = ProblemAdmin(Problem, admin.site)
        with patch.object(problem_admin, "message_user"), patch.object(
            problem_admin, "_rescore"
        ):
            problem_admin.make_public(
                self._request(self.admin), Problem.objects.filter(pk=self.problem.pk)
            )
        self.assertEqual(
            Problem.objects.filter(pk=self.problem.pk)
            .values_list("is_public", "is_locked")
            .get(),
            (True, False),
        )

    def test_admin_lock_option_is_superuser_only(self):
        for model, model_admin in (
            (Problem, ProblemAdmin),
            (Contest, ContestAdmin),
            (Quiz, QuizAdmin),
            (QuizQuestion, QuizQuestionAdmin),
            (BlogPost, BlogPostAdmin),
        ):
            instance = model_admin(model, admin.site)
            self.assertIn("is_locked", instance.get_readonly_fields(self._request()))
            self.assertNotIn(
                "is_locked", instance.get_readonly_fields(self._request(self.admin))
            )

    def test_superuser_direct_contest_publication_is_audited_without_fake_review(self):
        contest = Contest.objects.create(
            key="manualpublish",
            name="Manual",
            is_private=True,
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
        )
        with self.assertRaises(PermissionDenied):
            publish_without_review(contest, self.author, "Reason")
        with self.assertRaises(ValidationError):
            publish_without_review(contest, self.admin, "")
        request = self._request(
            self.admin, data={"override_reason": "Checked package manually"}
        )
        response = contest_review_accept(request, contest.key)
        self.assertEqual(response.status_code, 200)
        contest.refresh_from_db()
        self.assertTrue(contest.is_visible)
        self.assertFalse(contest.is_private)
        self.assertTrue(contest.is_locked)
        event = PublicationRequestEvent.objects.get(contest=contest)
        self.assertIsNone(event.contest_run_id)
        self.assertEqual(event.reason, "Checked package manually")
        self.assertFalse(ContestReviewRun.objects.filter(contest=contest).exists())

    def test_contest_request_and_decision_notifications_are_separate_from_running_review(
        self,
    ):
        contest = Contest.objects.create(
            key="requestnotification",
            name="Contest <script>unsafe</script>",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
        )
        contest.authors.add(self.profile)
        run = ContestReviewRun.objects.create(
            contest=contest,
            workflow_version=2,
            status="D",
            readiness="READY",
            content_hash="a" * 64,
        )
        row = submit_publication(contest, self.author, run.pk)
        submit_publication(contest, self.author, run.pk)
        notices = Notification.objects.filter(
            owner=self.admin_profile,
            category=NotificationCategory.CONTEST_PUBLIC_REQUEST_NEW,
        )
        self.assertEqual(notices.count(), 1)
        self.assertIn("?tab=overview#run-status", notices.get().html_link)
        self.assertNotIn("<script>", notices.get().html_link)
        self.assertFalse(contest.is_visible)
        decide_publication(
            contest, self.admin, approve=False, feedback="Please fix the set"
        )
        self.assertEqual(
            Notification.objects.filter(
                owner=self.profile,
                category=NotificationCategory.CONTEST_PUBLIC_REQUEST_REJECTED,
            ).count(),
            1,
        )
        with self.assertRaises(ValidationError):
            decide_publication(contest, self.admin, approve=False)
        row.refresh_from_db()
        self.assertEqual(row.status, ContestPublicRequest.REJECTED)
        submit_publication(contest, self.author, run.pk)
        # Publishing is exercised only inside this rolled-back test database.
        decide_publication(contest, self.admin, publish=True)
        self.assertEqual(
            Notification.objects.filter(
                owner=self.profile,
                category=NotificationCategory.CONTEST_PUBLIC_REQUEST_APPROVED,
            ).count(),
            1,
        )

    def test_bound_contest_request_remains_safe_when_feature_switch_is_disabled(self):
        contest = Contest.objects.create(
            key="bounddisabled",
            name="Bound disabled",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
        )
        contest.authors.add(self.profile)
        run = ContestReviewRun.objects.create(
            contest=contest,
            triggered_by=self.profile,
            workflow_version=2,
            content_hash="a" * 64,
            status=ContestReviewRun.DONE,
            readiness="READY",
        )
        row = submit_publication(contest, self.author, run.pk)
        with override_settings(AUTO_REVIEW_COORDINATOR_ENABLED=False), patch(
            "judge.review.publication.workflow.current_content_hash",
            return_value="c" * 64,
        ):
            response = contest_review_accept(self._request(self.admin), contest.key)
            self.assertEqual(response.status_code, 400)
        row.refresh_from_db()
        self.assertEqual(row.status, ContestPublicRequest.PENDING)
        with override_settings(AUTO_REVIEW_COORDINATOR_ENABLED=False):
            response = contest_request_cancel(self._request(), contest.key)
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.status, ContestPublicRequest.WITHDRAWN)

    def test_contest_publication_changes_actual_access_scope_without_publishing_children(
        self,
    ):
        contest = Contest.objects.create(
            key="restrictedpublish",
            name="Restricted",
            is_private=True,
            is_organization_private=True,
            is_in_course=True,
            start_time=timezone.now() + timedelta(hours=1),
            end_time=timezone.now() + timedelta(hours=2),
        )
        contest.authors.add(self.profile)
        ContestProblem.objects.create(
            contest=contest, problem=self.problem, points=1, order=0
        )
        review_run = ContestReviewRun.objects.create(
            contest=contest,
            triggered_by=self.profile,
            status=ContestReviewRun.DONE,
            workflow_version=2,
            content_hash="a" * 64,
            readiness="READY",
        )
        row = submit_publication(contest, self.admin, review_run.pk)
        with patch(
            "judge.review.publication.workflow.invalidate_readiness"
        ) as invalidate:
            with self.captureOnCommitCallbacks(execute=True):
                decide_publication(contest, self.admin, publish=True)
            invalidate.assert_called_once()
        contest.refresh_from_db()
        self.problem.refresh_from_db()
        self.assertTrue(contest.is_visible)
        self.assertTrue(contest.is_locked)
        self.assertFalse(contest.is_private)
        self.assertFalse(contest.is_organization_private)
        self.assertFalse(contest.is_in_course)
        self.assertFalse(self.problem.is_public)
        self.assertFalse(self.problem.is_locked)
        row.refresh_from_db()
        self.assertEqual(row.review_run_id, review_run.pk)
        self.assertEqual(row.candidate_hash, "a" * 64)
