import errno

from django.conf import settings
from django.contrib.contenttypes.fields import GenericRelation
from django.core.cache import cache
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import CASCADE, F, FilteredRelation, Q, SET_NULL, Exists, OuterRef
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from judge.fulltext import SearchManager
from judge.models.pagevote import PageVotable
from judge.models.bookmark import Bookmarkable
from judge.models.profile import Organization, Profile
from judge.models.runtime import Language
from judge.user_translations import gettext as user_gettext
from judge.models.problem_data import (
    problem_data_storage,
    problem_directory_file_helper,
)
from judge.caching import cache_wrapper, CacheableModel

__all__ = [
    "ProblemGroup",
    "ProblemType",
    "Problem",
    "ProblemTranslation",
    "License",
    "Solution",
]


def problem_directory_file(data, filename):
    return problem_directory_file_helper(data.code, filename)


class ProblemType(models.Model):
    name = models.CharField(
        max_length=20, verbose_name=_("problem category ID"), unique=True
    )
    full_name = models.CharField(
        max_length=100, verbose_name=_("problem category name")
    )

    def __str__(self):
        return self.full_name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        problem_ids = Problem.types.through.objects.filter(
            problemtype_id=self.id
        ).values_list("problem_id", flat=True)
        if problem_ids:
            _get_problem_types_name.dirty_multi(*problem_ids)

    save.alters_data = True

    class Meta:
        ordering = ["full_name"]
        verbose_name = _("problem type")
        verbose_name_plural = _("problem types")


class ProblemGroup(models.Model):
    name = models.CharField(
        max_length=20, verbose_name=_("problem group ID"), unique=True
    )
    full_name = models.CharField(max_length=100, verbose_name=_("problem group name"))

    def __str__(self):
        return self.full_name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        problem_ids = Problem.objects.filter(group=self).values_list("id", flat=True)
        if problem_ids:
            Problem.dirty_cache(*problem_ids)

    save.alters_data = True

    class Meta:
        ordering = ["full_name"]
        verbose_name = _("problem group")
        verbose_name_plural = _("problem groups")


class License(models.Model):
    key = models.CharField(
        max_length=20,
        unique=True,
        verbose_name=_("key"),
        validators=[RegexValidator(r"^[-\w.]+$", r"License key must be ^[-\w.]+$")],
    )
    link = models.CharField(max_length=256, verbose_name=_("link"))
    name = models.CharField(max_length=256, verbose_name=_("full name"))
    display = models.CharField(
        max_length=256,
        blank=True,
        verbose_name=_("short name"),
        help_text=_("Displayed on pages under this license"),
    )
    icon = models.CharField(
        max_length=256,
        blank=True,
        verbose_name=_("icon"),
        help_text=_("URL to the icon"),
    )
    text = models.TextField(verbose_name=_("license text"))

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("license", args=(self.key,))

    class Meta:
        verbose_name = _("license")
        verbose_name_plural = _("licenses")


class Problem(CacheableModel, PageVotable, Bookmarkable):
    code = models.CharField(
        max_length=20,
        verbose_name=_("problem code"),
        unique=True,
        validators=[
            RegexValidator("^[a-z0-9]+$", _("Problem code must be ^[a-z0-9]+$"))
        ],
        help_text=_(
            "A short, unique code for the problem, " "used in the url after /problem/"
        ),
    )
    name = models.CharField(
        max_length=100,
        verbose_name=_("problem name"),
        db_index=True,
        help_text=_("The full name of the problem, " "as shown in the problem list."),
    )
    description = models.TextField(verbose_name=_("problem body"), blank=True)
    authors = models.ManyToManyField(
        Profile,
        verbose_name=_("creators"),
        blank=True,
        related_name="authored_problems",
        help_text=_(
            "These users will be able to edit the problem, " "and be listed as authors."
        ),
    )
    curators = models.ManyToManyField(
        Profile,
        verbose_name=_("curators"),
        blank=True,
        related_name="curated_problems",
        help_text=_(
            "These users will be able to edit the problem, "
            "but not be listed as authors."
        ),
    )
    testers = models.ManyToManyField(
        Profile,
        verbose_name=_("testers"),
        blank=True,
        related_name="tested_problems",
        help_text=_(
            "These users will be able to view the private problem, but not edit it."
        ),
    )
    types = models.ManyToManyField(
        ProblemType,
        verbose_name=_("problem types"),
        help_text=_("The type of problem, " "as shown on the problem's page."),
    )
    group = models.ForeignKey(
        ProblemGroup,
        verbose_name=_("problem group"),
        on_delete=CASCADE,
        help_text=_("The group of problem, shown under Category in the problem list."),
    )
    time_limit = models.FloatField(
        verbose_name=_("time limit"),
        help_text=_(
            "The time limit for this problem, in seconds. "
            "Fractional seconds (e.g. 1.5) are supported."
        ),
        validators=[
            MinValueValidator(settings.DMOJ_PROBLEM_MIN_TIME_LIMIT),
            MaxValueValidator(settings.DMOJ_PROBLEM_MAX_TIME_LIMIT),
        ],
    )
    memory_limit = models.PositiveIntegerField(
        verbose_name=_("memory limit"),
        help_text=_(
            "The memory limit for this problem, in kilobytes "
            "(e.g. 256mb = 262144 kilobytes)."
        ),
        validators=[
            MinValueValidator(settings.DMOJ_PROBLEM_MIN_MEMORY_LIMIT),
            MaxValueValidator(settings.DMOJ_PROBLEM_MAX_MEMORY_LIMIT),
        ],
    )
    short_circuit = models.BooleanField(default=False)
    points = models.FloatField(
        verbose_name=_("points"),
        help_text=_(
            "Points awarded for problem completion. "
            "Points are displayed with a 'p' suffix if partial."
        ),
        validators=[MinValueValidator(settings.DMOJ_PROBLEM_MIN_PROBLEM_POINTS)],
    )
    partial = models.BooleanField(
        verbose_name=_("allows partial points"), default=False
    )
    allowed_languages = models.ManyToManyField(
        Language,
        verbose_name=_("allowed languages"),
        help_text=_("List of allowed submission languages."),
    )
    is_public = models.BooleanField(
        verbose_name=_("publicly visible"), db_index=True, default=False
    )
    is_manually_managed = models.BooleanField(
        verbose_name=_("manually managed"),
        db_index=True,
        default=False,
        help_text=_("Whether judges should be allowed to manage data or not."),
    )
    date = models.DateTimeField(
        verbose_name=_("date of publishing"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_(
            "Doesn't have magic ability to auto-publish due to backward compatibility"
        ),
    )
    banned_users = models.ManyToManyField(
        Profile,
        verbose_name=_("personae non gratae"),
        blank=True,
        help_text=_("Bans the selected users from submitting to this problem."),
    )
    license = models.ForeignKey(
        License,
        null=True,
        blank=True,
        on_delete=SET_NULL,
        help_text=_("The license under which this problem is published."),
    )
    og_image = models.CharField(
        verbose_name=_("OpenGraph image"), max_length=150, blank=True
    )
    summary = models.TextField(
        blank=True,
        verbose_name=_("problem summary"),
        help_text=_(
            "Plain-text, shown in meta description tag, e.g. for social media."
        ),
    )
    user_count = models.IntegerField(
        verbose_name=_("number of users"),
        default=0,
        help_text=_("The number of users who solved the problem."),
    )
    ac_rate = models.FloatField(verbose_name=_("solve rate"), default=0)

    tickets = GenericRelation("Ticket")
    comments = GenericRelation("Comment")
    pagevote = GenericRelation("PageVote")
    bookmark = GenericRelation("BookMark")
    objects = SearchManager(("code", "name"))

    organizations = models.ManyToManyField(
        Organization,
        blank=True,
        verbose_name=_("organizations"),
        help_text=_("If private, only these organizations may see the problem."),
    )
    is_organization_private = models.BooleanField(
        verbose_name=_("private to organizations"), default=False
    )
    pdf_description = models.FileField(
        verbose_name=_("pdf statement"),
        storage=problem_data_storage,
        null=True,
        blank=True,
        upload_to=problem_directory_file,
    )

    def __init__(self, *args, **kwargs):
        super(Problem, self).__init__(*args, **kwargs)
        self.__original_code = self.code

    def languages_list(self):
        common_names = set(
            [item["common_name"] for item in _get_allowed_languages(self.id)]
        )
        return sorted(common_names)

    def is_editor(self, profile):
        return (
            self.authors.filter(id=profile.id) | self.curators.filter(id=profile.id)
        ).exists()

    def is_editable_by(self, user):
        if not user.is_authenticated:
            return False
        if (
            user.has_perm("judge.edit_all_problem")
            or user.has_perm("judge.edit_public_problem")
            and self.is_public
        ):
            return True
        return user.has_perm("judge.edit_own_problem") and self.is_editor(user.profile)

    def is_accessible_by(self, user, in_contest_mode=True):
        # Problem is public.
        if self.is_public:
            # Problem is not private to an organization.
            if not self.is_organization_private:
                return True

            # If the user can see all organization private problems.
            if user.has_perm("judge.see_organization_problem"):
                return True

            # If the user is in the organization.
            if user.is_authenticated and self.organizations.filter(
                id__in=user.profile.organizations.all()
            ):
                return True

        # If the user can view all problems.
        if user.has_perm("judge.see_private_problem"):
            return True

        if not user.is_authenticated:
            return False

        # If the user authored the problem or is a curator.
        if user.has_perm("judge.edit_own_problem") and self.is_editor(user.profile):
            return True

        # If user is a tester.
        if self.testers.filter(id=user.profile.id).exists():
            return True

        # If user is currently in a contest containing that problem.
        current = user.profile.current_contest_id
        if not in_contest_mode or current is None:
            return False
        from judge.models import ContestProblem

        return ContestProblem.objects.filter(
            problem_id=self.id, contest__users__id=current
        ).exists()

    def is_subs_manageable_by(self, user):
        return (
            user.is_staff
            and user.has_perm("judge.rejudge_submission")
            and self.is_editable_by(user)
        )

    @classmethod
    def get_visible_problems(cls, user, profile=None):
        # Do unauthenticated check here so we can skip authentication checks later on.
        if not user.is_authenticated or not user:
            return cls.get_public_problems()

        # Conditions for visible problem:
        #   - `judge.edit_all_problem` or `judge.see_private_problem`
        #   - otherwise
        #       - not is_public problems
        #           - author or curator or tester
        #       - is_public problems
        #           - not is_organization_private or in organization or `judge.see_organization_problem`
        #           - author or curator or tester
        queryset = cls.objects.defer("description")
        profile = profile or user.profile
        if not (
            user.has_perm("judge.see_private_problem")
            or user.has_perm("judge.edit_all_problem")
        ):
            q = Q(is_public=True)
            if not user.has_perm("judge.see_organization_problem"):
                # Either not organization private or in the organization.
                q &= Q(is_organization_private=False) | Q(
                    is_organization_private=True,
                    organizations__in=profile.organizations.all(),
                )

            # Authors, curators, and testers should always have access, so OR at the very end.
            q |= Exists(
                Problem.authors.through.objects.filter(
                    problem=OuterRef("pk"), profile=profile
                )
            )
            q |= Exists(
                Problem.curators.through.objects.filter(
                    problem=OuterRef("pk"), profile=profile
                )
            )
            q |= Exists(
                Problem.testers.through.objects.filter(
                    problem=OuterRef("pk"), profile=profile
                )
            )
            queryset = queryset.filter(q)

        return queryset

    @classmethod
    def get_public_problems(cls):
        return cls.objects.filter(is_public=True, is_organization_private=False).defer(
            "description"
        )

    def __str__(self):
        return "%s (%s)" % (self.name, self.code)

    def get_absolute_url(self):
        return reverse("problem_detail", args=(self.get_code(),))

    @cache_wrapper(prefix="Pgai", expected_type=list)
    def get_author_ids(self):
        return list(
            Problem.authors.through.objects.filter(problem=self.id).values_list(
                "profile_id", flat=True
            )
        )

    @cache_wrapper(prefix="Pgci", expected_type=list)
    def get_curator_ids(self):
        return list(
            Problem.curators.through.objects.filter(problem=self).values_list(
                "profile_id", flat=True
            )
        )

    @cache_wrapper(prefix="Pgti", expected_type=list)
    def get_tester_ids(self):
        return list(
            Problem.testers.through.objects.filter(problem=self).values_list(
                "profile_id", flat=True
            )
        )

    def get_authors(self):
        return Profile.get_cached_instances(*self.get_author_ids())

    @cached_property
    def editor_ids(self):
        return list(set(self.get_author_ids() + self.get_curator_ids()))

    @cached_property
    def tester_ids(self):
        return self.get_tester_ids()

    @cached_property
    def usable_common_names(self):
        return set(self.usable_languages.values_list("common_name", flat=True))

    @property
    def usable_languages(self):
        return self.allowed_languages.filter(
            judges__in=self.judges.filter(online=True)
        ).distinct()

    def translated_name(self, language):
        translation = _get_problem_i18n_name(self.id, language)
        if not translation:
            return self.get_name()

    @classmethod
    def get_cached_dict(cls, problem_id):
        return _get_problem(problem_id)

    @classmethod
    def get_cached_instances(cls, *ids):
        _get_problem.batch([(id,) for id in ids])
        return [cls(id=id) for id in ids]

    @classmethod
    def prefetch_cache_i18n_name(cls, lang, *ids):
        _get_problem_i18n_name.batch([(id, lang) for id in ids])

    @classmethod
    def prefetch_cache_types_name(cls, *ids):
        _get_problem_types_name.batch([(id,) for id in ids])

    @classmethod
    def prefetch_cache_description(cls, lang, *ids):
        if lang:
            _get_problem_i18n_description.batch([(id, lang) for id in ids])
        else:
            _get_problem_description.batch([(id,) for id in ids])

    @classmethod
    def prefetch_cache_has_public_editorial(cls, *ids):
        _get_problem_has_public_editorial.batch([(id,) for id in ids])

    @classmethod
    def dirty_cache(cls, *ids):
        _get_problem.dirty_multi([(id,) for id in ids])
        _get_problem_description.dirty_multi([(id,) for id in ids])

    def get_code(self):
        return self.get_cached_value("code")

    def get_name(self):
        return self.get_cached_value("name")

    def get_time_limit(self):
        return self.get_cached_value("time_limit")

    def get_memory_limit(self):
        return self.get_cached_value("memory_limit")

    def get_points(self):
        return self.get_cached_value("points")

    def get_ac_rate(self):
        return self.get_cached_value("ac_rate")

    def get_user_count(self):
        return self.get_cached_value("user_count")

    def get_is_public(self):
        return self.get_cached_value("is_public")

    def get_group_name(self):
        return self.get_cached_value("group_name")

    def get_partial(self):
        return self.get_cached_value("partial")

    def get_description(self):
        return _get_problem_description(self.id)

    def translated_description(self, language):
        return _get_problem_i18n_description(self.id, language)

    def get_types_name(self):
        return _get_problem_types_name(self.id)

    def has_public_editorial(self):
        return _get_problem_has_public_editorial(self.id)

    def get_allowed_languages(self):
        return [item["id"] for item in _get_allowed_languages(self.id)]

    def get_organization_ids(self):
        return _get_problem_organization_ids(self.id)

    @classmethod
    def prefetch_organization_ids(cls, *problem_ids):
        _get_problem_organization_ids.batch([(id,) for id in problem_ids])

    def get_organizations(self):
        organization_ids = self.get_organization_ids()
        return Organization.get_cached_instances(*organization_ids)

    def get_contest_points(self, contest_id):
        from judge.models.contest import get_contest_problem_points

        points_dict = get_contest_problem_points(contest_id)
        return points_dict.get(self.id)

    def get_contest_user_count(self, contest_id):
        from judge.models.contest import get_contest_problem_user_count

        user_counts = get_contest_problem_user_count(contest_id)
        return user_counts.get(self.id, 0)

    def update_stats(self):
        self.user_count = (
            self.submission_set.filter(
                points__gte=self.points, result="AC", user__is_unlisted=False
            )
            .values("user")
            .distinct()
            .count()
        )
        submissions = self.submission_set.count()
        if submissions:
            self.ac_rate = (
                100.0
                * self.submission_set.filter(
                    points__gte=self.points, result="AC", user__is_unlisted=False
                ).count()
                / submissions
            )
        else:
            self.ac_rate = 0
        self.save()

    update_stats.alters_data = True

    def _get_limits(self, key):
        global_limit = getattr(self, key)
        limits = {
            limit["language_id"]: (limit["language__name"], limit[key])
            for limit in self.language_limits.values(
                "language_id", "language__name", key
            )
            if limit[key] != global_limit
        }
        limit_ids = set(limits.keys())
        common = []

        for cn, ids in Language.get_common_name_map().items():
            if ids - limit_ids:
                continue
            limit = set(limits[id][1] for id in ids)
            if len(limit) == 1:
                limit = next(iter(limit))
                common.append((cn, limit))
                for id in ids:
                    del limits[id]

        limits = list(limits.values()) + common
        limits.sort()
        return limits

    @property
    def language_time_limit(self):
        key = "problem_tls:%d" % self.id
        result = cache.get(key)
        if result is not None:
            return result
        result = self._get_limits("time_limit")
        cache.set(key, result)
        return result

    @property
    def language_memory_limit(self):
        key = "problem_mls:%d" % self.id
        result = cache.get(key)
        if result is not None:
            return result
        result = self._get_limits("memory_limit")
        cache.set(key, result)
        return result

    def handle_code_change(self):
        has_data = hasattr(self, "data_files")
        has_pdf = bool(self.pdf_description)
        if not has_data and not has_pdf:
            return

        try:
            problem_data_storage.rename(self.__original_code, self.code)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise

        if has_pdf:
            self.pdf_description.name = problem_directory_file_helper(
                self.code, self.pdf_description.name
            )
            super().save(update_fields=["pdf_description"])

        if has_data:
            self.data_files._update_code(self.__original_code, self.code)

    def save(self, should_move_data=True, *args, **kwargs):
        code_changed = self.__original_code and self.code != self.__original_code
        super(Problem, self).save(*args, **kwargs)
        if code_changed and should_move_data:
            self.handle_code_change()

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        problem_data_storage.delete_directory(self.code)

    save.alters_data = True
    delete.alters_data = True

    class Meta:
        permissions = (
            ("see_private_problem", "See hidden problems"),
            ("edit_own_problem", "Edit own problems"),
            ("edit_all_problem", "Edit all problems"),
            ("edit_public_problem", "Edit all public problems"),
            ("clone_problem", "Clone problem"),
            ("change_public_visibility", "Change is_public field"),
            ("change_manually_managed", "Change is_manually_managed field"),
            ("see_organization_problem", "See organization-private problems"),
            ("suggest_problem_changes", "Suggest changes to problem"),
        )
        verbose_name = _("problem")
        verbose_name_plural = _("problems")


class ProblemTranslation(models.Model):
    problem = models.ForeignKey(
        Problem,
        verbose_name=_("problem"),
        related_name="translations",
        on_delete=CASCADE,
    )
    language = models.CharField(
        verbose_name=_("language"), max_length=7, choices=settings.LANGUAGES
    )
    name = models.CharField(
        verbose_name=_("translated name"), max_length=100, db_index=True
    )
    description = models.TextField(verbose_name=_("translated description"))

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        _get_problem_i18n_name.dirty(self.problem_id, self.language)
        _get_problem_i18n_description.dirty(self.problem_id, self.language)

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        _get_problem_i18n_name.dirty(self.problem_id, self.language)
        _get_problem_i18n_description.dirty(self.problem_id, self.language)

    save.alters_data = True

    class Meta:
        unique_together = ("problem", "language")
        verbose_name = _("problem translation")
        verbose_name_plural = _("problem translations")


class LanguageLimit(models.Model):
    problem = models.ForeignKey(
        Problem,
        verbose_name=_("problem"),
        related_name="language_limits",
        on_delete=CASCADE,
    )
    language = models.ForeignKey(
        Language, verbose_name=_("language"), on_delete=CASCADE
    )
    time_limit = models.FloatField(
        verbose_name=_("time limit"),
        validators=[
            MinValueValidator(settings.DMOJ_PROBLEM_MIN_TIME_LIMIT),
            MaxValueValidator(settings.DMOJ_PROBLEM_MAX_TIME_LIMIT),
        ],
    )
    memory_limit = models.IntegerField(
        verbose_name=_("memory limit"),
        validators=[
            MinValueValidator(settings.DMOJ_PROBLEM_MIN_MEMORY_LIMIT),
            MaxValueValidator(settings.DMOJ_PROBLEM_MAX_MEMORY_LIMIT),
        ],
    )

    class Meta:
        unique_together = ("problem", "language")
        verbose_name = _("language-specific resource limit")
        verbose_name_plural = _("language-specific resource limits")


class LanguageTemplate(models.Model):
    problem = models.ForeignKey(
        Problem,
        verbose_name=_("problem"),
        related_name="language_templates",
        on_delete=CASCADE,
    )
    language = models.ForeignKey(
        Language, verbose_name=_("language"), on_delete=CASCADE
    )
    source = models.TextField(verbose_name=_("source code"), max_length=65536)

    class Meta:
        unique_together = ("problem", "language")
        verbose_name = _("language-specific template")
        verbose_name_plural = _("language-specific templates")


class Solution(models.Model, PageVotable, Bookmarkable):
    problem = models.OneToOneField(
        Problem,
        on_delete=CASCADE,
        verbose_name=_("associated problem"),
        null=True,
        blank=True,
        related_name="solution",
    )
    is_public = models.BooleanField(verbose_name=_("public visibility"), default=False)
    publish_on = models.DateTimeField(verbose_name=_("publish date"))
    authors = models.ManyToManyField(Profile, verbose_name=_("authors"), blank=True)
    content = models.TextField(verbose_name=_("editorial content"))
    comments = GenericRelation("Comment")
    pagevote = GenericRelation("PageVote")
    bookmark = GenericRelation("BookMark")

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.problem:
            # Invalidate the has_public_editorial cache
            _get_problem_has_public_editorial.dirty((self.problem_id,))

    save.alters_data = True

    def get_absolute_url(self):
        problem = self.problem
        if problem is None:
            return reverse("home")
        else:
            return reverse("problem_editorial", args=[problem.code])

    @cache_wrapper(prefix="Sga", expected_type=models.query.QuerySet)
    def get_authors(self):
        return self.authors.only("id")

    def __str__(self):
        return _("Editorial for %s") % self.problem.name

    class Meta:
        permissions = (("see_private_solution", "See hidden solutions"),)
        verbose_name = _("solution")
        verbose_name_plural = _("solutions")


class ProblemPointsVote(models.Model):
    points = models.IntegerField(
        verbose_name=_("proposed point value"),
        help_text=_("The amount of points you think this problem deserves."),
        validators=[
            MinValueValidator(100),
            MaxValueValidator(600),
        ],
    )

    voter = models.ForeignKey(
        Profile, related_name="problem_points_votes", on_delete=CASCADE, db_index=True
    )
    problem = models.ForeignKey(
        Problem, related_name="problem_points_votes", on_delete=CASCADE, db_index=True
    )
    vote_time = models.DateTimeField(
        verbose_name=_("The time this vote was cast"),
        auto_now_add=True,
        blank=True,
    )

    class Meta:
        verbose_name = _("vote")
        verbose_name_plural = _("votes")

    def __str__(self):
        return f"{self.voter}: {self.points} for {self.problem.code}"


@receiver(m2m_changed, sender=Problem.organizations.through)
def update_organization_problem(sender, instance, **kwargs):
    if kwargs["action"] in ["post_add", "post_remove", "post_clear"]:
        instance.is_organization_private = instance.organizations.exists()
        instance.save(update_fields=["is_organization_private"])
        _get_problem_organization_ids.dirty(instance.id)


def _get_problem_batch(args_list):
    """
    Batch function to get problem data for multiple problems at once.

    Args:
        args_list: List of tuples, where each tuple contains a problem_id

    Returns:
        List of problem data dictionaries
    """
    # Extract problem IDs from args_list
    problem_ids = [args[0] for args in args_list]

    # Fetch all problems in a single query with appropriate related data using values
    problems = (
        Problem.objects.filter(id__in=problem_ids)
        .select_related("group")
        .values(
            "id",
            "code",
            "name",
            "time_limit",
            "memory_limit",
            "points",
            "partial",
            "is_public",
            "is_organization_private",
            "group_id",
            "group__full_name",
            "user_count",
            "ac_rate",
        )
    )

    # Create a dictionary mapping problem_id to problem data
    problem_dict = {}
    for problem in problems:
        problem_id = problem["id"]
        problem_dict[problem_id] = {
            "code": problem["code"],
            "name": problem["name"],
            "time_limit": problem["time_limit"],
            "memory_limit": problem["memory_limit"],
            "points": problem["points"],
            "partial": problem["partial"],
            "is_public": problem["is_public"],
            "is_organization_private": problem["is_organization_private"],
            "group_id": problem["group_id"],
            "group_name": problem["group__full_name"],
            "user_count": problem["user_count"],
            "ac_rate": problem["ac_rate"],
        }
        # Remove None values to save cache space
        problem_dict[problem_id] = {
            k: v for k, v in problem_dict[problem_id].items() if v is not None
        }

    # Build result list in the same order as the input problem_ids
    results = []
    for problem_id in problem_ids:
        if problem_id in problem_dict:
            results.append(problem_dict[problem_id])
        else:
            assert False, f"Invalid problem_id, {problem_id}"

    return results


@cache_wrapper(prefix="Prgp2", expected_type=dict, batch_fn=_get_problem_batch)
def _get_problem(problem_id):
    results = _get_problem_batch([(problem_id,)])
    return results[0]


def _get_problem_description_batch(args_list):
    """
    Batch function to get problem descriptions for multiple problems at once.

    Args:
        args_list: List of tuples, where each tuple contains a problem_id

    Returns:
        List of problem descriptions
    """
    problem_ids = [args[0] for args in args_list]

    descriptions = Problem.objects.filter(id__in=problem_ids).values(
        "id", "description"
    )
    description_dict = {item["id"]: item["description"] for item in descriptions}

    results = []
    for problem_id in problem_ids:
        if problem_id in description_dict:
            results.append(description_dict[problem_id])
        else:
            results.append("")

    return results


@cache_wrapper(
    prefix="Prdesc", expected_type=str, batch_fn=_get_problem_description_batch
)
def _get_problem_description(problem_id):
    results = _get_problem_description_batch([(problem_id,)])
    return results[0]


def _get_problem_i18n_name_batch(args_list):
    """
    Batch function to get translated problem names for multiple problems in a specific language.

    Args:
        args_list: List of tuples, where each tuple contains (problem_id, language)

    Returns:
        List of translated problem names
    """
    problems_by_lang = {}
    for problem_id, language in args_list:
        if language not in problems_by_lang:
            problems_by_lang[language] = []
        problems_by_lang[language].append(problem_id)

    results_dict = {}

    for language, problem_ids in problems_by_lang.items():
        translations = ProblemTranslation.objects.filter(
            problem_id__in=problem_ids, language=language
        ).values("problem_id", "name")

        problem_id_to_name = {}

        for trans in translations:
            problem_id_to_name[trans["problem_id"]] = trans["name"]

        for problem_id in problem_ids:
            results_dict[(problem_id, language)] = problem_id_to_name.get(problem_id)

    results = []
    for problem_id, language in args_list:
        results.append(results_dict.get((problem_id, language), None))

    return results


@cache_wrapper(
    prefix="Pri18n2", expected_type=str, batch_fn=_get_problem_i18n_name_batch
)
def _get_problem_i18n_name(problem_id, language):
    results = _get_problem_i18n_name_batch([(problem_id, language)])
    return results[0]


def _get_problem_i18n_description_batch(args_list):
    """
    Batch function to get translated problem descriptions for multiple problems in a specific language.

    Args:
        args_list: List of tuples, where each tuple contains (problem_id, language)

    Returns:
        List of translated problem descriptions
    """
    problems_by_lang = {}
    for problem_id, language in args_list:
        if language not in problems_by_lang:
            problems_by_lang[language] = []
        problems_by_lang[language].append(problem_id)

    results_dict = {}

    for language, problem_ids in problems_by_lang.items():
        problem_descriptions = {
            p["id"]: p["description"]
            for p in Problem.objects.filter(id__in=problem_ids).values(
                "id", "description"
            )
        }

        translations = ProblemTranslation.objects.filter(
            problem_id__in=problem_ids, language=language
        ).values("problem_id", "description")

        for trans in translations:
            problem_descriptions[trans["problem_id"]] = trans["description"]

        for problem_id in problem_ids:
            if problem_id in problem_descriptions:
                results_dict[(problem_id, language)] = problem_descriptions[problem_id]
            else:
                results_dict[(problem_id, language)] = None

    results = []
    for problem_id, language in args_list:
        results.append(results_dict.get((problem_id, language), None))

    return results


@cache_wrapper(
    prefix="Pri18ndesc", expected_type=str, batch_fn=_get_problem_i18n_description_batch
)
def _get_problem_i18n_description(problem_id, language):
    results = _get_problem_i18n_description_batch([(problem_id, language)])
    return results[0]


def _get_problem_types_name_batch(args_list):
    """
    Batch function to get problem types' full names for multiple problems at once.

    Args:
        args_list: List of tuples, where each tuple contains a problem_id

    Returns:
        List of lists, each containing the full names of the problem types
    """
    problem_ids = [args[0] for args in args_list]
    problem_types = {}

    # Fetch problem type relationships for all problems in one query
    problem_type_relations = Problem.types.through.objects.filter(
        problem_id__in=problem_ids
    ).values("problem_id", "problemtype_id")

    # Get all unique type IDs
    type_ids = set(rel["problemtype_id"] for rel in problem_type_relations)

    # Fetch all type information in one query
    types_info = {
        t["id"]: t["full_name"]
        for t in ProblemType.objects.filter(id__in=type_ids).values("id", "full_name")
    }

    # Group types by problem
    for relation in problem_type_relations:
        problem_id = relation["problem_id"]
        type_id = relation["problemtype_id"]

        if problem_id not in problem_types:
            problem_types[problem_id] = []

        if type_id in types_info:
            problem_types[problem_id].append(types_info[type_id])

    # Build result list in the same order as the input problem_ids
    results = []
    for problem_id in problem_ids:
        results.append(problem_types.get(problem_id, []))

    return results


@cache_wrapper(
    prefix="Prtn", expected_type=list, batch_fn=_get_problem_types_name_batch
)
def _get_problem_types_name(problem_id):
    results = _get_problem_types_name_batch([(problem_id,)])
    return results[0]


def _get_problem_has_public_editorial_batch(args_list):
    """
    Batch function to check if problems have public editorials.

    Args:
        args_list: List of tuples, where each tuple contains a problem_id

    Returns:
        List of booleans indicating whether each problem has a public editorial
    """
    # Extract problem IDs from args_list
    problem_ids = [args[0] for args in args_list]

    # Get all problem IDs that have public editorials
    problem_ids_with_editorial = set(
        Solution.objects.filter(
            problem_id__in=problem_ids, is_public=True, publish_on__lte=now()
        ).values_list("problem_id", flat=True)
    )

    # Build result list in the same order as the input problem_ids
    results = []
    for problem_id in problem_ids:
        results.append(problem_id in problem_ids_with_editorial)
    return results


@cache_wrapper(prefix="Prhe", batch_fn=_get_problem_has_public_editorial_batch)
def _get_problem_has_public_editorial(problem_id):
    results = _get_problem_has_public_editorial_batch([(problem_id,)])
    return results[0]


@cache_wrapper(prefix="problem_distinct_points", timeout=1800, expected_type=list)
def get_distinct_problem_points():
    return sorted(Problem.objects.values_list("points", flat=True).distinct())


@cache_wrapper(prefix="problem_types", timeout=1800, expected_type=list)
def get_all_problem_types():
    return list(ProblemType.objects.values("id", "full_name"))


@cache_wrapper(prefix="problem_groups", timeout=1800, expected_type=list)
def get_all_problem_groups():
    return list(ProblemGroup.objects.values("id", "full_name"))


@cache_wrapper(prefix="Pgaln", expected_type=list)
def _get_allowed_languages(problem_id):
    return list(
        Problem.objects.get(id=problem_id).allowed_languages.values("id", "common_name")
    )


def _get_problem_organization_ids_batch(args_list):
    """
    Batch function to get organization IDs for multiple problems efficiently.

    Args:
        args_list: List of tuples, each containing a single problem_id

    Returns:
        List of organization ID lists, one for each problem_id in args_list
    """
    # Extract problem IDs from args_list
    problem_ids = [args[0] for args in args_list]

    # Direct query to the through table to avoid JOIN
    through_model = Problem.organizations.through
    query = through_model.objects.filter(problem_id__in=problem_ids)

    # Group organization IDs by problem ID
    problem_orgs = {}
    for problem_id, org_id in query.values_list("problem_id", "organization_id"):
        if problem_id not in problem_orgs:
            problem_orgs[problem_id] = []
        problem_orgs[problem_id].append(org_id)

    # Return results in the same order as input problem_ids
    results = []
    for problem_id in problem_ids:
        results.append(problem_orgs.get(problem_id, []))

    return results


@cache_wrapper(
    prefix="Prgoi", expected_type=list, batch_fn=_get_problem_organization_ids_batch
)
def _get_problem_organization_ids(problem_id):
    """Get organization IDs for a problem"""
    results = _get_problem_organization_ids_batch([(problem_id,)])
    return results[0]
