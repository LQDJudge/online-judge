from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.db.models import CASCADE, Q, Count, Max, Min
from django.db.models.signals import m2m_changed
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext, gettext_lazy as _
from django.contrib.contenttypes.fields import GenericRelation
from django.dispatch import receiver

from jsonfield import JSONField
from lupa import LuaRuntime
from moss import (
    MOSS_LANG_C,
    MOSS_LANG_CC,
    MOSS_LANG_JAVA,
    MOSS_LANG_PYTHON,
    MOSS_LANG_PASCAL,
)
from datetime import timedelta, datetime

from judge import contest_format
from judge.models.problem import Problem
from judge.models.profile import Organization, Profile
from judge.models.submission import Submission
from judge.ratings import rate_contest
from judge.models.pagevote import PageVotable
from judge.models.bookmark import Bookmarkable
from judge.fulltext import SearchManager
from judge.caching import cache_wrapper

__all__ = [
    "Contest",
    "ContestTag",
    "ContestParticipation",
    "ContestProblem",
    "ContestSubmission",
    "Rating",
    "ContestProblemClarification",
    "ContestsSummary",
    "OfficialContest",
    "OfficialContestCategory",
    "OfficialContestLocation",
    "get_contest_problem_ids",
    "get_global_rating_range",
    "get_user_rating_stats",
]


class ContestTag(models.Model):
    color_validator = RegexValidator("^#(?:[A-Fa-f0-9]{3}){1,2}$", _("Invalid colour."))

    name = models.CharField(
        max_length=20,
        verbose_name=_("tag name"),
        unique=True,
        validators=[
            RegexValidator(
                r"^[a-z-]+$", message=_("Lowercase letters and hyphens only.")
            )
        ],
    )
    color = models.CharField(
        max_length=7, verbose_name=_("tag colour"), validators=[color_validator]
    )
    description = models.TextField(verbose_name=_("tag description"), blank=True)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("contest_tag", args=[self.name])

    @property
    def text_color(self, cache={}):
        if self.color not in cache:
            if len(self.color) == 4:
                r, g, b = [ord(bytes.fromhex(i * 2)) for i in self.color[1:]]
            else:
                r, g, b = [i for i in bytes.fromhex(self.color[1:])]
            cache[self.color] = (
                "#000" if 299 * r + 587 * g + 144 * b > 140000 else "#fff"
            )
        return cache[self.color]

    class Meta:
        verbose_name = _("contest tag")
        verbose_name_plural = _("contest tags")


class Contest(models.Model, PageVotable, Bookmarkable):
    SCOREBOARD_VISIBLE = "V"
    SCOREBOARD_AFTER_CONTEST = "C"
    SCOREBOARD_AFTER_PARTICIPATION = "P"
    SCOREBOARD_VISIBILITY = (
        (SCOREBOARD_VISIBLE, _("Visible")),
        (SCOREBOARD_AFTER_CONTEST, _("Hidden for duration of contest")),
        (SCOREBOARD_AFTER_PARTICIPATION, _("Hidden for duration of participation")),
    )
    key = models.CharField(
        max_length=20,
        verbose_name=_("contest id"),
        unique=True,
        validators=[RegexValidator("^[a-z0-9]+$", _("Contest id must be ^[a-z0-9]+$"))],
    )
    name = models.CharField(
        max_length=100, verbose_name=_("contest name"), db_index=True
    )
    authors = models.ManyToManyField(
        Profile,
        verbose_name=_("authors"),
        help_text=_("These users will be able to edit the contest."),
        related_name="authors+",
    )
    curators = models.ManyToManyField(
        Profile,
        verbose_name=_("curators"),
        help_text=_(
            "These users will be able to edit the contest, "
            "but will not be listed as authors."
        ),
        related_name="curators+",
        blank=True,
    )
    testers = models.ManyToManyField(
        Profile,
        verbose_name=_("testers"),
        help_text=_(
            "These users will be able to view the contest, " "but not edit it."
        ),
        blank=True,
        related_name="testers+",
    )
    description = models.TextField(verbose_name=_("description"), blank=True)
    problems = models.ManyToManyField(
        Problem, verbose_name=_("problems"), through="ContestProblem"
    )
    start_time = models.DateTimeField(verbose_name=_("start time"), db_index=True)
    end_time = models.DateTimeField(verbose_name=_("end time"), db_index=True)
    time_limit = models.DurationField(
        verbose_name=_("time limit"),
        blank=True,
        null=True,
        help_text=_(
            "Format hh:mm:ss. For example, if you want a 2-hour contest, enter 02:00:00"
        ),
    )
    freeze_after = models.DurationField(
        verbose_name=_("freeze after"),
        blank=True,
        null=True,
        help_text=_(
            "Format hh:mm:ss. For example, if you want to freeze contest after 2 hours, enter 02:00:00"
        ),
    )
    is_visible = models.BooleanField(
        verbose_name=_("publicly visible"),
        default=False,
        help_text=_(
            "Should be set even for organization-private contests, where it "
            "determines whether the contest is visible to members of the "
            "specified organizations."
        ),
    )
    is_rated = models.BooleanField(
        verbose_name=_("contest rated"),
        help_text=_("Whether this contest can be rated."),
        default=False,
    )
    scoreboard_visibility = models.CharField(
        verbose_name=_("scoreboard visibility"),
        default=SCOREBOARD_VISIBLE,
        max_length=1,
        help_text=_("Scoreboard visibility through the duration " "of the contest"),
        choices=SCOREBOARD_VISIBILITY,
    )
    view_contest_scoreboard = models.ManyToManyField(
        Profile,
        verbose_name=_("view contest scoreboard"),
        blank=True,
        related_name="view_contest_scoreboard",
        help_text=_("These users will be able to view the scoreboard."),
    )
    public_scoreboard = models.BooleanField(
        verbose_name=_("public scoreboard"),
        help_text=_("Ranking page is public even for private contests."),
        default=False,
    )
    use_clarifications = models.BooleanField(
        verbose_name=_("no comments"),
        help_text=_("Use clarification system instead of comments."),
        default=True,
    )
    rating_floor = models.IntegerField(
        verbose_name=("rating floor"),
        help_text=_("Rating floor for contest"),
        null=True,
        blank=True,
    )
    rating_ceiling = models.IntegerField(
        verbose_name=("rating ceiling"),
        help_text=_("Rating ceiling for contest"),
        null=True,
        blank=True,
    )
    rate_all = models.BooleanField(
        verbose_name=_("rate all"),
        help_text=_("Rate all users who joined."),
        default=False,
    )
    rate_exclude = models.ManyToManyField(
        Profile,
        verbose_name=_("exclude from ratings"),
        blank=True,
        related_name="rate_exclude+",
    )
    is_private = models.BooleanField(
        verbose_name=_("private to specific users"), default=False
    )
    private_contestants = models.ManyToManyField(
        Profile,
        blank=True,
        verbose_name=_("private contestants"),
        help_text=_("If private, only these users may see the contest"),
        related_name="private_contestants+",
    )
    hide_problem_tags = models.BooleanField(
        verbose_name=_("hide problem tags"),
        help_text=_("Whether problem tags should be hidden by default."),
        default=True,
    )
    run_pretests_only = models.BooleanField(
        verbose_name=_("run pretests only"),
        help_text=_(
            "Whether judges should grade pretests only, versus all "
            "testcases. Commonly set during a contest, then unset "
            "prior to rejudging user submissions when the contest ends."
        ),
        default=False,
    )
    is_organization_private = models.BooleanField(
        verbose_name=_("private to organizations"), default=False
    )
    organizations = models.ManyToManyField(
        Organization,
        blank=True,
        verbose_name=_("organizations"),
        help_text=_("If private, only these organizations may see the contest"),
    )
    is_in_course = models.BooleanField(
        verbose_name=_("contest in course"),
        default=False,
    )
    og_image = models.CharField(
        verbose_name=_("OpenGraph image"), default="", max_length=150, blank=True
    )
    logo_override_image = models.CharField(
        verbose_name=_("Logo override image"),
        default="",
        max_length=150,
        blank=True,
        help_text=_(
            "This image will replace the default site logo for users "
            "inside the contest."
        ),
    )
    tags = models.ManyToManyField(
        ContestTag, verbose_name=_("contest tags"), blank=True, related_name="contests"
    )
    user_count = models.IntegerField(
        verbose_name=_("the amount of live participants"), default=0
    )
    summary = models.TextField(
        blank=True,
        verbose_name=_("contest summary"),
        help_text=_(
            "Plain-text, shown in meta description tag, e.g. for social media."
        ),
    )
    access_code = models.CharField(
        verbose_name=_("access code"),
        blank=True,
        default="",
        max_length=255,
        help_text=_(
            "An optional code to prompt contestants before they are allowed "
            "to join the contest. Leave it blank to disable."
        ),
    )
    banned_users = models.ManyToManyField(
        Profile,
        verbose_name=_("personae non gratae"),
        blank=True,
        help_text=_("Bans the selected users from joining this contest."),
    )
    format_name = models.CharField(
        verbose_name=_("contest format"),
        default="default",
        max_length=32,
        choices=contest_format.choices(),
        help_text=_("The contest format module to use."),
    )
    format_config = JSONField(
        verbose_name=_("contest format configuration"),
        null=True,
        blank=True,
        help_text=_(
            "A JSON object to serve as the configuration for the chosen contest format "
            "module. Leave empty to use None. Exact format depends on the contest format "
            "selected."
        ),
    )
    problem_label_script = models.TextField(
        verbose_name="contest problem label script",
        blank=True,
        help_text="A custom Lua function to generate problem labels. Requires a "
        "single function with an integer parameter, the zero-indexed "
        "contest problem index, and returns a string, the label.",
    )
    points_precision = models.IntegerField(
        verbose_name=_("precision points"),
        default=2,
        validators=[MinValueValidator(0), MaxValueValidator(10)],
        help_text=_("Number of digits to round points to."),
    )
    rate_limit = models.PositiveIntegerField(
        verbose_name=(_("rate limit")),
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text=_(
            "Maximum number of submissions per minute. Leave empty if you don't want rate limit."
        ),
    )
    comments = GenericRelation("Comment")
    pagevote = GenericRelation("PageVote")
    bookmark = GenericRelation("BookMark")
    objects = SearchManager(("key", "name"))

    @cached_property
    def format_class(self):
        return contest_format.formats[self.format_name]

    @cached_property
    def format(self):
        return self.format_class(self, self.format_config)

    @cached_property
    def get_label_for_problem(self):
        def DENY_ALL(obj, attr_name, is_setting):
            raise AttributeError()

        lua = LuaRuntime(
            attribute_filter=DENY_ALL, register_eval=False, register_builtins=False
        )
        return lua.eval(
            self.problem_label_script or self.format.get_contest_problem_label_script()
        )

    def clean(self):
        # Django will complain if you didn't fill in start_time or end_time, so we don't have to.
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValidationError(_("End time must be after start time"))
        self.format_class.validate(self.format_config)

        try:
            # a contest should have at least one problem, with contest problem index 0
            # so test it to see if the script returns a valid label.
            label = self.get_label_for_problem(0)
        except Exception as e:
            raise ValidationError("Contest problem label script: %s" % e)
        else:
            if not isinstance(label, str):
                raise ValidationError(
                    "Contest problem label script: script should return a string."
                )

    def save(self, *args, **kwargs):
        earliest_start_time = datetime(1999, 5, 4).replace(tzinfo=timezone.utc)
        if self.start_time < earliest_start_time:
            self.start_time = earliest_start_time

        # If start_time is more than a year from now, set it to a year from now
        now = timezone.now()
        one_year_from_now = now + timedelta(days=365)
        if self.start_time > one_year_from_now:
            self.start_time = one_year_from_now

        if self.end_time < self.start_time:
            self.end_time = self.start_time + timedelta(hours=1)

        one_year_later = self.start_time + timedelta(days=365)
        if self.end_time > one_year_later:
            self.end_time = one_year_later

        max_duration = timedelta(days=7)
        if self.time_limit and self.time_limit > max_duration:
            self.time_limit = max_duration

        super().save(*args, **kwargs)

    def is_in_contest(self, user):
        if user.is_authenticated:
            profile = user.profile
            return (
                profile
                and profile.current_contest is not None
                and profile.current_contest.contest == self
            )
        return False

    def can_see_own_scoreboard(self, user):
        if self.can_see_full_scoreboard(user):
            return True
        if not self.can_join:
            return False
        if not self.show_scoreboard and not self.is_in_contest(user):
            return False
        return True

    def can_see_full_scoreboard(self, user):
        if self.show_scoreboard:
            return True
        if not user.is_authenticated:
            return False
        if user.has_perm("judge.see_private_contest") or user.has_perm(
            "judge.edit_all_contest"
        ):
            return True
        if user.profile.id in self.editor_ids:
            return True
        if self.view_contest_scoreboard.filter(id=user.profile.id).exists():
            return True
        if (
            self.scoreboard_visibility == self.SCOREBOARD_AFTER_PARTICIPATION
            and self.has_completed_contest(user)
        ):
            return True
        return False

    def has_completed_contest(self, user):
        if user.is_authenticated:
            participation = self.users.filter(
                virtual=ContestParticipation.LIVE, user=user.profile
            ).first()
            if participation and participation.ended:
                return True
        return False

    @cached_property
    def show_scoreboard(self):
        if not self.can_join:
            return False
        if (
            self.scoreboard_visibility
            in (self.SCOREBOARD_AFTER_CONTEST, self.SCOREBOARD_AFTER_PARTICIPATION)
            and not self.ended
        ):
            return False
        return True

    @property
    def contest_window_length(self):
        return self.end_time - self.start_time

    @cached_property
    def _now(self):
        # This ensures that all methods talk about the same now.
        return timezone.now()

    @cached_property
    def can_join(self):
        return self.start_time <= self._now

    @property
    def time_before_start(self):
        if self.start_time >= self._now:
            return self.start_time - self._now
        else:
            return None

    @property
    def time_before_end(self):
        if self.end_time >= self._now:
            return self.end_time - self._now
        else:
            return None

    @cached_property
    def ended(self):
        return self.end_time < self._now

    @cache_wrapper(prefix="Coai")
    def _author_ids(self):
        return set(
            Contest.authors.through.objects.filter(contest=self).values_list(
                "profile_id", flat=True
            )
        )

    @cache_wrapper(prefix="Coci")
    def _curator_ids(self):
        return set(
            Contest.curators.through.objects.filter(contest=self).values_list(
                "profile_id", flat=True
            )
        )

    @cache_wrapper(prefix="Coti")
    def _tester_ids(self):
        return set(
            Contest.testers.through.objects.filter(contest=self).values_list(
                "profile_id", flat=True
            )
        )

    @cached_property
    def author_ids(self):
        return self._author_ids()

    @cached_property
    def editor_ids(self):
        return self.author_ids.union(self._curator_ids())

    @cached_property
    def tester_ids(self):
        return self._tester_ids()

    def get_organization_ids(self):
        return _get_contest_organization_ids(self.id)

    @classmethod
    def prefetch_organization_ids(cls, *contest_ids):
        """Prefetch organization IDs for multiple contests"""
        _get_contest_organization_ids.batch([(id,) for id in contest_ids])

    def get_organizations(self):
        organization_ids = self.get_organization_ids()
        return Organization.get_cached_instances(*organization_ids)

    def __str__(self):
        return f"{self.name} ({self.key})"

    def get_absolute_url(self):
        return reverse("contest_view", args=(self.key,))

    def update_user_count(self):
        self.user_count = self.users.filter(virtual=0).count()
        self.save()

    update_user_count.alters_data = True

    class Inaccessible(Exception):
        pass

    class PrivateContest(Exception):
        pass

    def access_check(self, user):
        # Do unauthenticated check here so we can skip authentication checks later on.
        if not user.is_authenticated:
            # Unauthenticated users can only see visible, non-private contests
            if not self.is_visible:
                raise self.Inaccessible()
            if self.is_private or self.is_organization_private:
                raise self.PrivateContest()
            return

        # If the user can view or edit all contests
        if user.has_perm("judge.see_private_contest") or user.has_perm(
            "judge.edit_all_contest"
        ):
            return

        # User is organizer or curator for contest
        if user.profile.id in self.editor_ids:
            return

        # User is tester for contest
        if user.profile.id in self.tester_ids:
            return

        # Contest is not publicly visible
        if not self.is_visible:
            raise self.Inaccessible()

        if self.is_in_course:
            from judge.models import Course, CourseContest

            course_contest = CourseContest.objects.filter(contest=self).first()
            if Course.is_accessible_by(course_contest.course, user.profile):
                return
            raise self.Inaccessible()

        # Contest is not private
        if not self.is_private and not self.is_organization_private:
            return

        if self.view_contest_scoreboard.filter(id=user.profile.id).exists():
            return

        in_org = self.organizations.filter(
            id__in=user.profile.organizations.all()
        ).exists()
        in_users = self.private_contestants.filter(id=user.profile.id).exists()

        if not self.is_private and self.is_organization_private:
            if in_org:
                return
            raise self.PrivateContest()

        if self.is_private and not self.is_organization_private:
            if in_users:
                return
            raise self.PrivateContest()

        if self.is_private and self.is_organization_private:
            if in_org and in_users:
                return
            raise self.PrivateContest()

    def is_accessible_by(self, user):
        try:
            self.access_check(user)
        except (self.Inaccessible, self.PrivateContest):
            return False
        else:
            return True

    def is_editable_by(self, user):
        # If the user can edit all contests
        if user.has_perm("judge.edit_all_contest"):
            return True

        # If the user is a contest organizer or curator
        if hasattr(user, "profile") and user.profile.id in self.editor_ids:
            return True

        return False

    @classmethod
    def get_visible_contests(cls, user, show_own_contests_only=False):
        if not user.is_authenticated:
            return (
                cls.objects.filter(
                    is_visible=True,
                    is_organization_private=False,
                    is_private=False,
                    is_in_course=False,
                )
                .defer("description")
                .distinct()
            )

        queryset = cls.objects.defer("description")
        if (
            not (
                user.has_perm("judge.see_private_contest")
                or user.has_perm("judge.edit_all_contest")
            )
            or show_own_contests_only
        ):
            q = Q(is_visible=True, is_in_course=False)
            q &= (
                Q(view_contest_scoreboard=user.profile)
                | Q(is_organization_private=False, is_private=False)
                | Q(
                    is_organization_private=False,
                    is_private=True,
                    private_contestants=user.profile,
                )
                | Q(
                    is_organization_private=True,
                    is_private=False,
                    organizations__in=user.profile.organizations.all(),
                )
                | Q(
                    is_organization_private=True,
                    is_private=True,
                    organizations__in=user.profile.organizations.all(),
                    private_contestants=user.profile,
                )
            )

            q |= Q(authors=user.profile)
            q |= Q(curators=user.profile)
            q |= Q(testers=user.profile)
            queryset = queryset.filter(q)
        return queryset.distinct()

    def rate(self):
        Rating.objects.filter(
            contest__end_time__range=(self.end_time, self._now)
        ).delete()
        for contest in Contest.objects.filter(
            is_rated=True,
            end_time__range=(self.end_time, self._now),
        ).order_by("end_time"):
            rate_contest(contest)

    class Meta:
        permissions = (
            ("see_private_contest", _("See private contests")),
            ("edit_own_contest", _("Edit own contests")),
            ("edit_all_contest", _("Edit all contests")),
            ("clone_contest", _("Clone contest")),
            ("moss_contest", _("MOSS contest")),
            ("contest_rating", _("Rate contests")),
            ("contest_access_code", _("Contest access codes")),
            ("create_private_contest", _("Create private contests")),
            ("change_contest_visibility", _("Change contest visibility")),
            ("contest_problem_label", _("Edit contest problem label script")),
        )
        verbose_name = _("contest")
        verbose_name_plural = _("contests")


@receiver(m2m_changed, sender=Contest.organizations.through)
def update_organization_private(sender, instance, **kwargs):
    if kwargs["action"] in ["post_add", "post_remove", "post_clear"]:
        instance.is_organization_private = instance.organizations.exists()
        instance.save(update_fields=["is_organization_private"])


@receiver(m2m_changed, sender=Contest.private_contestants.through)
def update_private(sender, instance, **kwargs):
    if kwargs["action"] in ["post_add", "post_remove", "post_clear"]:
        instance.is_private = instance.private_contestants.exists()
        instance.save(update_fields=["is_private"])


@receiver(m2m_changed, sender=Contest.organizations.through)
def on_contest_organization_change(sender, instance, action, **kwargs):
    if action in ["post_add", "post_remove", "post_clear"]:
        if isinstance(instance, Contest):
            _get_contest_organization_ids.dirty((instance.id,))


class ContestParticipation(models.Model):
    LIVE = 0
    SPECTATE = -1

    contest = models.ForeignKey(
        Contest,
        verbose_name=_("associated contest"),
        related_name="users",
        on_delete=CASCADE,
    )
    user = models.ForeignKey(
        Profile,
        verbose_name=_("user"),
        related_name="contest_history",
        on_delete=CASCADE,
    )
    real_start = models.DateTimeField(
        verbose_name=_("start time"), default=timezone.now, db_column="start"
    )
    score = models.FloatField(verbose_name=_("score"), default=0, db_index=True)
    cumtime = models.PositiveIntegerField(verbose_name=_("cumulative time"), default=0)
    is_disqualified = models.BooleanField(
        verbose_name=_("is disqualified"),
        default=False,
        help_text=_("Whether this participation is disqualified."),
    )
    tiebreaker = models.FloatField(verbose_name=_("tie-breaking field"), default=0.0)
    virtual = models.IntegerField(
        verbose_name=_("virtual participation id"),
        default=LIVE,
        help_text=_("0 means non-virtual, otherwise the n-th virtual participation."),
    )
    format_data = JSONField(
        verbose_name=_("contest format specific data"), null=True, blank=True
    )
    format_data_final = JSONField(
        verbose_name=_("same as format_data, but including frozen results"),
        null=True,
        blank=True,
    )
    score_final = models.FloatField(verbose_name=_("final score"), default=0)
    cumtime_final = models.PositiveIntegerField(
        verbose_name=_("final cumulative time"), default=0
    )

    def recompute_results(self):
        with transaction.atomic():
            self.contest.format.update_participation(self)
            if self.is_disqualified:
                self.score = -9999
                self.save(update_fields=["score"])

    recompute_results.alters_data = True

    def set_disqualified(self, disqualified):
        self.is_disqualified = disqualified
        self.recompute_results()
        if self.contest.is_rated and self.contest.ratings.exists():
            self.contest.rate()
        if self.is_disqualified:
            if self.user.current_contest == self:
                self.user.remove_contest()
            self.contest.banned_users.add(self.user)
        else:
            self.contest.banned_users.remove(self.user)

    set_disqualified.alters_data = True

    @property
    def live(self):
        return self.virtual == self.LIVE

    @property
    def spectate(self):
        return self.virtual == self.SPECTATE

    @cached_property
    def start(self):
        contest = self.contest
        return (
            contest.start_time
            if contest.time_limit is None and (self.live or self.spectate)
            else self.real_start
        )

    @cached_property
    def end_time(self):
        contest = self.contest
        if self.spectate:
            return contest.end_time
        if self.virtual:
            if contest.time_limit:
                return self.real_start + contest.time_limit
            else:
                return self.real_start + (contest.end_time - contest.start_time)
        return (
            contest.end_time
            if contest.time_limit is None
            else min(self.real_start + contest.time_limit, contest.end_time)
        )

    @cached_property
    def _now(self):
        # This ensures that all methods talk about the same now.
        return timezone.now()

    @property
    def ended(self):
        return self.end_time is not None and self.end_time < self._now

    @property
    def time_remaining(self):
        end = self.end_time
        if end is not None and end >= self._now:
            return end - self._now

    def __str__(self):
        if self.spectate:
            return gettext("%s spectating in %s") % (
                self.username,
                self.contest.name,
            )
        if self.virtual:
            return gettext("%s in %s, v%d") % (
                self.username,
                self.contest.name,
                self.virtual,
            )
        return gettext("%s in %s") % (self.username, self.contest.name)

    class Meta:
        verbose_name = _("contest participation")
        verbose_name_plural = _("contest participations")

        unique_together = ("contest", "user", "virtual")


class ContestProblem(models.Model):
    problem = models.ForeignKey(
        Problem, verbose_name=_("problem"), related_name="contests", on_delete=CASCADE
    )
    contest = models.ForeignKey(
        Contest,
        verbose_name=_("contest"),
        related_name="contest_problems",
        on_delete=CASCADE,
    )
    points = models.IntegerField(verbose_name=_("points"))
    partial = models.BooleanField(default=True, verbose_name=_("partial"))
    is_pretested = models.BooleanField(default=False, verbose_name=_("is pretested"))
    order = models.PositiveIntegerField(db_index=True, verbose_name=_("order"))
    show_testcases = models.BooleanField(
        verbose_name=_("visible testcases"),
        default=False,
    )
    max_submissions = models.IntegerField(
        help_text=_(
            "Maximum number of submissions for this problem, " "or 0 for no limit."
        ),
        verbose_name=_("max submissions"),
        default=0,
        validators=[
            MinValueValidator(0, _("Why include a problem you " "can't submit to?"))
        ],
    )
    hidden_subtasks = models.CharField(
        help_text=_("Separated by commas, e.g: 2, 3"),
        verbose_name=_("hidden subtasks"),
        null=True,
        blank=True,
        max_length=20,
    )

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Invalidate the cache when a contest problem is updated
        get_contest_problem_points.dirty(self.contest_id)
        get_contest_problem_ids.dirty(self.contest_id)

    save.alters_data = True

    def delete(self, *args, **kwargs):
        contest_id = self.contest_id
        super().delete(*args, **kwargs)
        # Invalidate the cache when a contest problem is deleted
        get_contest_problem_points.dirty(contest_id)
        get_contest_problem_ids.dirty(contest_id)

    delete.alters_data = True

    @property
    def clarifications(self):
        return ContestProblemClarification.objects.filter(problem=self)

    class Meta:
        unique_together = ("problem", "contest")
        verbose_name = _("contest problem")
        verbose_name_plural = _("contest problems")


class ContestSubmission(models.Model):
    submission = models.OneToOneField(
        Submission,
        verbose_name=_("submission"),
        related_name="contest",
        on_delete=CASCADE,
    )
    problem = models.ForeignKey(
        ContestProblem,
        verbose_name=_("problem"),
        on_delete=CASCADE,
        related_name="submissions",
        related_query_name="submission",
    )
    participation = models.ForeignKey(
        ContestParticipation,
        verbose_name=_("participation"),
        on_delete=CASCADE,
        related_name="submissions",
        related_query_name="submission",
    )
    points = models.FloatField(default=0.0, verbose_name=_("points"))
    is_pretest = models.BooleanField(
        verbose_name=_("is pretested"),
        help_text=_("Whether this submission was ran only on pretests."),
        default=False,
    )

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Invalidate the user count cache when a submission is added or updated
        get_contest_problem_user_count.dirty(self.problem.contest_id)

    save.alters_data = True

    def delete(self, *args, **kwargs):
        contest_id = self.problem.contest_id
        super().delete(*args, **kwargs)
        # Invalidate the cache when a submission is deleted
        get_contest_problem_user_count.dirty(contest_id)

    delete.alters_data = True

    class Meta:
        verbose_name = _("contest submission")
        verbose_name_plural = _("contest submissions")


class Rating(models.Model):
    user = models.ForeignKey(
        Profile, verbose_name=_("user"), related_name="ratings", on_delete=CASCADE
    )
    contest = models.ForeignKey(
        Contest, verbose_name=_("contest"), related_name="ratings", on_delete=CASCADE
    )
    participation = models.OneToOneField(
        ContestParticipation,
        verbose_name=_("participation"),
        related_name="rating",
        on_delete=CASCADE,
    )
    rank = models.IntegerField(verbose_name=_("rank"))
    rating = models.IntegerField(verbose_name=_("rating"))
    mean = models.FloatField(verbose_name=_("raw rating"))
    performance = models.FloatField(verbose_name=_("contest performance"))
    last_rated = models.DateTimeField(db_index=True, verbose_name=_("last rated"))

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Invalidate user rating stats cache
        get_user_rating_stats.dirty(self.user_id)
        # Invalidate global rating range cache
        get_global_rating_range.dirty()

    def delete(self, *args, **kwargs):
        user_id = self.user_id
        super().delete(*args, **kwargs)
        # Invalidate caches after deletion
        get_user_rating_stats.dirty(user_id)
        get_global_rating_range.dirty()

    save.alters_data = True
    delete.alters_data = True

    class Meta:
        unique_together = ("user", "contest")
        verbose_name = _("contest rating")
        verbose_name_plural = _("contest ratings")


class ContestMoss(models.Model):
    LANG_MAPPING = [
        ("C", MOSS_LANG_C),
        ("C++", MOSS_LANG_CC),
        ("Java", MOSS_LANG_JAVA),
        ("Python", MOSS_LANG_PYTHON),
        ("Pascal", MOSS_LANG_PASCAL),
    ]

    contest = models.ForeignKey(
        Contest, verbose_name=_("contest"), related_name="moss", on_delete=CASCADE
    )
    problem = models.ForeignKey(
        Problem, verbose_name=_("problem"), related_name="moss", on_delete=CASCADE
    )
    language = models.CharField(max_length=10)
    submission_count = models.PositiveIntegerField(default=0)
    url = models.URLField(null=True, blank=True)

    class Meta:
        unique_together = ("contest", "problem", "language")
        verbose_name = _("contest moss result")
        verbose_name_plural = _("contest moss results")


class ContestProblemClarification(models.Model):
    problem = models.ForeignKey(
        ContestProblem, verbose_name=_("clarified problem"), on_delete=CASCADE
    )
    description = models.TextField(verbose_name=_("clarification body"))
    date = models.DateTimeField(
        verbose_name=_("clarification timestamp"), auto_now_add=True
    )


class ContestsSummary(models.Model):
    contests = models.ManyToManyField(
        Contest,
    )
    scores = models.JSONField(
        null=True,
        blank=True,
    )
    key = models.CharField(
        max_length=20,
        unique=True,
    )
    results = models.JSONField(null=True, blank=True)

    class Meta:
        verbose_name = _("contests summary")
        verbose_name_plural = _("contests summaries")

    def __str__(self):
        return self.key

    def get_absolute_url(self):
        return reverse("contests_summary", args=[self.key])


class OfficialContestCategory(models.Model):
    name = models.CharField(
        max_length=50, verbose_name=_("official contest category"), unique=True
    )

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = _("official contest category")
        verbose_name_plural = _("official contest categories")


class OfficialContestLocation(models.Model):
    name = models.CharField(
        max_length=50, verbose_name=_("official contest location"), unique=True
    )

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = _("official contest location")
        verbose_name_plural = _("official contest locations")


class OfficialContest(models.Model):
    contest = models.OneToOneField(
        Contest,
        verbose_name=_("contest"),
        related_name="official",
        on_delete=CASCADE,
    )
    category = models.ForeignKey(
        OfficialContestCategory,
        verbose_name=_("contest category"),
        on_delete=CASCADE,
    )
    year = models.PositiveIntegerField(verbose_name=_("year"))
    location = models.ForeignKey(
        OfficialContestLocation,
        verbose_name=_("contest location"),
        on_delete=CASCADE,
    )

    class Meta:
        verbose_name = _("official contest")
        verbose_name_plural = _("official contests")


@cache_wrapper(prefix="contest_problem_points", expected_type=dict)
def get_contest_problem_points(contest_id):
    return {
        cp["problem_id"]: cp["points"]
        for cp in ContestProblem.objects.filter(contest_id=contest_id).values(
            "problem_id", "points"
        )
    }


@cache_wrapper(prefix="contest_problem_id", expected_type=list)
def get_contest_problem_ids(contest_id):
    """
    Get a list of problem IDs for a given contest.

    Args:
        contest_id: The ID of the contest

    Returns:
        A list of problem IDs associated with the contest
    """
    return list(
        ContestProblem.objects.filter(contest_id=contest_id)
        .order_by("order")
        .values_list("problem_id", flat=True)
    )


@cache_wrapper(prefix="contest_problem_user_count", expected_type=dict)
def get_contest_problem_user_count(contest_id):
    """
    Get the number of unique users who submitted to each problem in a contest.

    Args:
        contest_id: The ID of the contest

    Returns:
        A dictionary mapping problem_id to the count of users who submitted
    """
    user_counts = (
        ContestProblem.objects.filter(contest_id=contest_id)
        .annotate(user_count=Count("submission__participation", distinct=True))
        .values("problem_id", "user_count")
    )

    return {item["problem_id"]: item["user_count"] for item in user_counts}


def _get_contest_organization_ids_batch(args_list):
    """
    Batch function to get organization IDs for multiple contests efficiently.

    Args:
        args_list: List of tuples, each containing a single contest_id

    Returns:
        List of organization ID lists, one for each contest_id in args_list
    """
    # Extract contest IDs from args_list
    contest_ids = [args[0] for args in args_list]

    # Direct query to the through table to avoid JOIN
    through_model = Contest.organizations.through
    query = through_model.objects.filter(contest_id__in=contest_ids)

    # Group organization IDs by contest ID
    contest_orgs = {}
    for contest_id, org_id in query.values_list("contest_id", "organization_id"):
        if contest_id not in contest_orgs:
            contest_orgs[contest_id] = []
        contest_orgs[contest_id].append(org_id)

    # Return results in the same order as input contest_ids
    results = []
    for contest_id in contest_ids:
        results.append(contest_orgs.get(contest_id, []))

    return results


@cache_wrapper(
    prefix="Cgoi", expected_type=list, batch_fn=_get_contest_organization_ids_batch
)
def _get_contest_organization_ids(contest_id):
    """Get organization IDs for a contest"""
    results = _get_contest_organization_ids_batch([(contest_id,)])
    return results[0]


@cache_wrapper(prefix="RTG_range", expected_type=dict)
def get_global_rating_range():
    """
    Get the global minimum and maximum rating values.

    Returns:
        A dictionary with keys 'rating__min' and 'rating__max'.
    """
    return Rating.objects.aggregate(Min("rating"), Max("rating"))


@cache_wrapper(prefix="RTG_user", expected_type=dict)
def get_user_rating_stats(profile_id):
    """
    Get a user's rating statistics.

    Args:
        profile_id: The ID of the user profile

    Returns:
        A dictionary with keys 'min_rating', 'max_rating', and 'contests' (count)
    """
    return Rating.objects.filter(user_id=profile_id).aggregate(
        min_rating=Min("rating"),
        max_rating=Max("rating"),
        contests=Count("contest"),
    )
