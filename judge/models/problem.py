import errno
from operator import attrgetter

from django.conf import settings
from django.contrib.contenttypes.fields import GenericRelation
from django.core.cache import cache
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import CASCADE, F, FilteredRelation, Q, SET_NULL, Exists, OuterRef
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from judge.fulltext import SearchQuerySet
from judge.models.pagevote import PageVotable
from judge.models.bookmark import Bookmarkable
from judge.models.profile import Organization, Profile
from judge.models.runtime import Language
from judge.user_translations import gettext as user_gettext
from judge.models.problem_data import (
    problem_data_storage,
    problem_directory_file_helper,
)
from judge.caching import cache_wrapper

__all__ = [
    "ProblemGroup",
    "ProblemType",
    "Problem",
    "ProblemTranslation",
    "License",
    "Solution",
    "TranslatedProblemQuerySet",
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


class TranslatedProblemQuerySet(SearchQuerySet):
    def __init__(self, **kwargs):
        super(TranslatedProblemQuerySet, self).__init__(("code", "name"), **kwargs)

    def add_i18n_name(self, language):
        return self.annotate(
            i18n_translation=FilteredRelation(
                "translations",
                condition=Q(translations__language=language),
            )
        ).annotate(
            i18n_name=Coalesce(
                F("i18n_translation__name"), F("name"), output_field=models.CharField()
            )
        )


class Problem(models.Model, PageVotable, Bookmarkable):
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

    objects = TranslatedProblemQuerySet.as_manager()
    tickets = GenericRelation("Ticket")
    comments = GenericRelation("Comment")
    pagevote = GenericRelation("PageVote")
    bookmark = GenericRelation("BookMark")

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
        self._translated_name_cache = {}
        self._i18n_name = None
        self.__original_code = self.code

    @cached_property
    def types_list(self):
        return list(map(user_gettext, map(attrgetter("full_name"), self.types.all())))

    def languages_list(self):
        return (
            self.allowed_languages.values_list("common_name", flat=True)
            .distinct()
            .order_by("common_name")
        )

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
        return reverse("problem_detail", args=(self.code,))

    @cached_property
    def author_ids(self):
        return Problem.authors.through.objects.filter(problem=self).values_list(
            "profile_id", flat=True
        )

    @cache_wrapper(prefix="Pga", expected_type=models.query.QuerySet)
    def get_authors(self):
        return self.authors.only("id")

    @cached_property
    def editor_ids(self):
        return self.author_ids.union(
            Problem.curators.through.objects.filter(problem=self).values_list(
                "profile_id", flat=True
            )
        )

    @cached_property
    def tester_ids(self):
        return Problem.testers.through.objects.filter(problem=self).values_list(
            "profile_id", flat=True
        )

    @cached_property
    def usable_common_names(self):
        return set(self.usable_languages.values_list("common_name", flat=True))

    @property
    def usable_languages(self):
        return self.allowed_languages.filter(
            judges__in=self.judges.filter(online=True)
        ).distinct()

    def translated_name(self, language):
        if language in self._translated_name_cache:
            return self._translated_name_cache[language]
        # Hits database despite prefetch_related.
        try:
            name = self.translations.filter(language=language).values_list(
                "name", flat=True
            )[0]
        except IndexError:
            name = self.name
        self._translated_name_cache[language] = name
        return name

    @property
    def i18n_name(self):
        if self._i18n_name is None:
            self._i18n_name = self._trans[0].name if self._trans else self.name
        return self._i18n_name

    @i18n_name.setter
    def i18n_name(self, value):
        self._i18n_name = value

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
def update_organization_private(sender, instance, **kwargs):
    if kwargs["action"] in ["post_add", "post_remove", "post_clear"]:
        instance.is_organization_private = instance.organizations.exists()
        instance.save(update_fields=["is_organization_private"])
