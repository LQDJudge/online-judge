from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from judge.models import Language, Problem, ProblemGroup, Profile


@override_settings(
    USE_ML=False,
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class ProblemEditorTest(TestCase):
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
        user = User.objects.create_user(username="problem_editor")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": language}
        )
        group = ProblemGroup.objects.create(name="editor", full_name="Editor Tests")
        cls.problem = Problem.objects.create(
            code="editor_target",
            name="Editor Target",
            group=group,
            time_limit=1,
            memory_limit=65536,
            points=1,
        )
        other = Problem.objects.create(
            code="editor_other",
            name="Other Problem",
            group=group,
            time_limit=1,
            memory_limit=65536,
            points=1,
        )
        other.authors.add(cls.profile)
        other.curators.add(cls.profile)

    def setUp(self):
        for getter in (
            Problem.get_author_ids,
            Problem.get_curator_ids,
            Problem.get_tester_ids,
        ):
            getter.dirty(self.problem)

    def test_unrelated_relationships_do_not_grant_edit_permission(self):
        with CaptureQueriesContext(connection) as queries:
            self.assertFalse(self.problem.is_editor(self.profile))
        self.assertEqual(len(queries), 2)
        for query in queries:
            self.assertNotIn("JOIN", query["sql"].upper())
        with self.assertNumQueries(0):
            self.assertFalse(self.problem.is_editor(self.profile))

    def test_author_check_short_circuits_and_revocation_takes_effect(self):
        self.problem.authors.add(self.profile)
        with self.assertNumQueries(1):
            self.assertTrue(self.problem.is_editor(self.profile))
        with self.assertNumQueries(0):
            self.assertTrue(self.problem.is_editor(self.profile))
        self.problem.authors.remove(self.profile)
        self.assertFalse(self.problem.is_editor(self.profile))

    def test_curator_can_edit_but_tester_cannot(self):
        self.problem.testers.add(self.profile)
        self.assertFalse(self.problem.is_editor(self.profile))
        self.problem.curators.add(self.profile)
        with self.assertNumQueries(1):
            self.assertTrue(self.problem.is_editor(self.profile))
        with self.assertNumQueries(0):
            self.assertTrue(self.problem.is_editor(self.profile))
        self.problem.curators.clear()
        self.assertFalse(self.problem.is_editor(self.profile))

    def test_relationship_id_caches_refresh_after_changes(self):
        for name in ("authors", "curators", "testers"):
            with self.subTest(relation=name):
                relation = getattr(self.problem, name)
                getter = getattr(self.problem, "get_%s_ids" % name[:-1])
                getter.dirty(self.problem)
                self.assertEqual(getter(), [])
                relation.add(self.profile)
                self.assertEqual(getter(), [self.profile.pk])
                relation.remove(self.profile)
                self.assertEqual(getter(), [])
                relation.add(self.profile)
                self.assertEqual(getter(), [self.profile.pk])
                relation.clear()
                self.assertEqual(getter(), [])
