from django.test import TestCase
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from judge.models import (
    Language,
    Organization,
    Profile,
    BlogPost,
    Problem,
    ProblemGroup,
    Solution,
    Contest,
)
from judge.models.pagevote import PageVote, PageVoteVoter, VoteService
from judge.models.comment import Comment
from judge.utils.contribution import compute_contribution, is_content_public


class ContributionTestCase(TestCase):
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
        cls.problem_group, _ = ProblemGroup.objects.get_or_create(
            name="General",
            defaults={"full_name": "General"},
        )

    def setUp(self):
        self.author_user = User.objects.create_user(
            username="author", password="password123"
        )
        self.author_profile, _ = Profile.objects.get_or_create(
            user=self.author_user, defaults={"language": self.language}
        )

        self.voter_user = User.objects.create_user(
            username="voter", password="password123"
        )
        self.voter_profile, _ = Profile.objects.get_or_create(
            user=self.voter_user, defaults={"language": self.language}
        )

        self.admin_user = User.objects.create_user(
            username="admin", password="password123"
        )
        self.admin_profile, _ = Profile.objects.get_or_create(
            user=self.admin_user, defaults={"language": self.language}
        )

    def _create_public_blog(self, title="Test Blog", org_private=False, orgs=None):
        blog = BlogPost.objects.create(
            title=title,
            slug="test-blog",
            visible=True,
            publish_on=timezone.now(),
            content="Test content",
            is_organization_private=org_private,
        )
        blog.authors.add(self.author_profile)
        if orgs:
            blog.organizations.set(orgs)
        return blog

    def _create_public_problem(self, code="TEST"):
        problem = Problem.objects.create(
            code=code,
            name="Test Problem",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=10.0,
            is_public=True,
            is_organization_private=False,
        )
        problem.authors.add(self.author_profile)
        problem.allowed_languages.set(Language.objects.all())
        return problem

    def _create_public_solution(self, problem):
        solution = Solution.objects.create(
            problem=problem,
            is_public=True,
            publish_on=timezone.now(),
            content="Editorial content",
        )
        solution.authors.add(self.author_profile)
        return solution

    def _create_public_contest(self, key="test-contest"):
        contest = Contest.objects.create(
            key=key,
            name="Test Contest",
            is_visible=True,
            is_private=False,
            is_organization_private=False,
            is_in_course=False,
            start_time=timezone.now(),
            end_time=timezone.now() + timezone.timedelta(hours=2),
        )
        contest.authors.add(self.author_profile)
        return contest

    def _add_pagevote(self, obj, score):
        ct = ContentType.objects.get_for_model(obj)
        pagevote, _ = PageVote.objects.get_or_create(content_type=ct, object_id=obj.pk)
        PageVoteVoter.objects.create(
            pagevote=pagevote, voter=self.voter_profile, score=score
        )
        pagevote.score = score
        pagevote.save()

    def _add_comment(self, obj, author_profile, score=0):
        ct = ContentType.objects.get_for_model(obj)
        comment = Comment.objects.create(
            author=author_profile,
            content_type=ct,
            object_id=obj.pk,
            body="Test comment",
            score=score,
        )
        return comment


class ComputeContributionTest(ContributionTestCase):
    def test_no_content_returns_zero(self):
        self.assertEqual(compute_contribution(self.author_profile), 0)

    def test_public_blog_pagevote(self):
        blog = self._create_public_blog()
        self._add_pagevote(blog, 3)
        self.assertEqual(compute_contribution(self.author_profile), 3)

    def test_public_problem_pagevote(self):
        problem = self._create_public_problem()
        self._add_pagevote(problem, 5)
        self.assertEqual(compute_contribution(self.author_profile), 5)

    def test_public_solution_pagevote(self):
        problem = self._create_public_problem(code="SOL1")
        solution = self._create_public_solution(problem)
        self._add_pagevote(solution, 2)
        self.assertEqual(compute_contribution(self.author_profile), 2)

    def test_public_contest_pagevote(self):
        contest = self._create_public_contest()
        self._add_pagevote(contest, 4)
        self.assertEqual(compute_contribution(self.author_profile), 4)

    def test_comment_score_on_public_content(self):
        blog = self._create_public_blog()
        self._add_comment(blog, self.author_profile, score=2)
        self.assertEqual(compute_contribution(self.author_profile), 2)

    def test_comment_on_others_content(self):
        """Comments by the user on other people's public content should count."""
        blog = self._create_public_blog()
        # Voter writes a comment on author's blog
        self._add_comment(blog, self.voter_profile, score=3)
        self.assertEqual(compute_contribution(self.voter_profile), 3)

    def test_combined_pagevote_and_comment(self):
        blog = self._create_public_blog()
        self._add_pagevote(blog, 5)
        self._add_comment(blog, self.author_profile, score=2)
        self.assertEqual(compute_contribution(self.author_profile), 7)

    def test_hidden_comment_excluded(self):
        blog = self._create_public_blog()
        comment = self._add_comment(blog, self.author_profile, score=3)
        comment.hidden = True
        comment.save()
        self.assertEqual(compute_contribution(self.author_profile), 0)

    def test_private_blog_excluded(self):
        """Org-private blog in non-community org should not count."""
        org = Organization.objects.create(
            name="Private Org",
            slug="private-org",
            short_name="PO",
            about="Private",
            registrant=self.admin_profile,
            is_community=False,
        )
        blog = self._create_public_blog(
            title="Private Blog", org_private=True, orgs=[org]
        )
        self._add_pagevote(blog, 5)
        self.assertEqual(compute_contribution(self.author_profile), 0)

    def test_community_blog_included(self):
        """Org-private blog in community org should count."""
        community = Organization.objects.create(
            name="Test Community",
            slug="test-community",
            short_name="TC",
            about="Community",
            registrant=self.admin_profile,
            is_community=True,
        )
        blog = self._create_public_blog(
            title="Community Blog", org_private=True, orgs=[community]
        )
        self._add_pagevote(blog, 3)
        self.assertEqual(compute_contribution(self.author_profile), 3)

    def test_private_problem_excluded(self):
        problem = Problem.objects.create(
            code="PRIV",
            name="Private Problem",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=10.0,
            is_public=True,
            is_organization_private=True,
        )
        problem.authors.add(self.author_profile)
        problem.allowed_languages.set(Language.objects.all())
        self._add_pagevote(problem, 5)
        self.assertEqual(compute_contribution(self.author_profile), 0)

    def test_course_contest_excluded(self):
        contest = Contest.objects.create(
            key="course-contest",
            name="Course Contest",
            is_visible=True,
            is_private=False,
            is_organization_private=False,
            is_in_course=True,
            start_time=timezone.now(),
            end_time=timezone.now() + timezone.timedelta(hours=2),
        )
        contest.authors.add(self.author_profile)
        self._add_pagevote(contest, 5)
        self.assertEqual(compute_contribution(self.author_profile), 0)


class IsContentPublicTest(ContributionTestCase):
    def test_public_blog(self):
        blog = self._create_public_blog()
        ct = ContentType.objects.get_for_model(BlogPost)
        self.assertTrue(is_content_public(ct, blog.pk))

    def test_org_private_non_community_blog(self):
        org = Organization.objects.create(
            name="Org",
            slug="org",
            short_name="O",
            about="Org",
            registrant=self.admin_profile,
            is_community=False,
        )
        blog = self._create_public_blog(org_private=True, orgs=[org])
        ct = ContentType.objects.get_for_model(BlogPost)
        self.assertFalse(is_content_public(ct, blog.pk))

    def test_community_blog(self):
        community = Organization.objects.create(
            name="Comm",
            slug="comm",
            short_name="C",
            about="Comm",
            registrant=self.admin_profile,
            is_community=True,
        )
        blog = self._create_public_blog(org_private=True, orgs=[community])
        ct = ContentType.objects.get_for_model(BlogPost)
        self.assertTrue(is_content_public(ct, blog.pk))

    def test_public_problem(self):
        problem = self._create_public_problem(code="PUB1")
        ct = ContentType.objects.get_for_model(Problem)
        self.assertTrue(is_content_public(ct, problem.pk))

    def test_private_contest(self):
        contest = Contest.objects.create(
            key="priv-contest",
            name="Private Contest",
            is_visible=True,
            is_private=True,
            is_organization_private=False,
            is_in_course=False,
            start_time=timezone.now(),
            end_time=timezone.now() + timezone.timedelta(hours=2),
        )
        ct = ContentType.objects.get_for_model(Contest)
        self.assertFalse(is_content_public(ct, contest.pk))


class DeltaUpdateTest(ContributionTestCase):
    def test_pagevote_delta_updates_contribution(self):
        blog = self._create_public_blog()
        Profile.objects.filter(id=self.author_profile.id).update(contribution_points=0)

        VoteService.vote(blog, self.voter_user, 1)
        self.author_profile.refresh_from_db()
        self.assertEqual(self.author_profile.contribution_points, 1)

    def test_pagevote_remove_updates_contribution(self):
        blog = self._create_public_blog()
        Profile.objects.filter(id=self.author_profile.id).update(contribution_points=0)

        VoteService.vote(blog, self.voter_user, 1)
        VoteService.vote(blog, self.voter_user, 0)
        self.author_profile.refresh_from_db()
        self.assertEqual(self.author_profile.contribution_points, 0)

    def test_pagevote_change_vote_updates_contribution(self):
        blog = self._create_public_blog()
        Profile.objects.filter(id=self.author_profile.id).update(contribution_points=0)

        VoteService.vote(blog, self.voter_user, 1)
        VoteService.vote(blog, self.voter_user, -1)
        self.author_profile.refresh_from_db()
        self.assertEqual(self.author_profile.contribution_points, -1)

    def test_private_content_no_delta(self):
        """Voting on private content should not update contribution."""
        org = Organization.objects.create(
            name="Org2",
            slug="org2",
            short_name="O2",
            about="Org2",
            registrant=self.admin_profile,
            is_community=False,
        )
        blog = self._create_public_blog(org_private=True, orgs=[org])
        Profile.objects.filter(id=self.author_profile.id).update(contribution_points=0)

        VoteService.vote(blog, self.voter_user, 1)
        self.author_profile.refresh_from_db()
        self.assertEqual(self.author_profile.contribution_points, 0)


class RecomputeContributionTest(ContributionTestCase):
    def test_recompute_matches_compute(self):
        """Recompute should produce the same result as compute_contribution."""
        blog = self._create_public_blog()
        self._add_pagevote(blog, 5)
        self._add_comment(blog, self.author_profile, score=3)

        expected = compute_contribution(self.author_profile)
        self.assertEqual(expected, 8)

        # Simulate drift: set to wrong value
        Profile.objects.filter(id=self.author_profile.id).update(
            contribution_points=999
        )
        self.author_profile.refresh_from_db()
        self.assertEqual(self.author_profile.contribution_points, 999)

        # Recompute
        new_points = compute_contribution(self.author_profile)
        Profile.objects.filter(id=self.author_profile.id).update(
            contribution_points=new_points
        )
        self.author_profile.refresh_from_db()
        self.assertEqual(self.author_profile.contribution_points, 8)
