from operator import mul
import os
from datetime import datetime

from django.conf import settings
from django.contrib.auth.models import User
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Max, CASCADE
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django.dispatch import receiver
from django.db.models.signals import post_save, pre_save, m2m_changed
from django.http import Http404

from fernet_fields import EncryptedCharField
from sortedm2m.fields import SortedManyToManyField

from judge.models.choices import ACE_THEMES, TIMEZONE
from judge.models.runtime import Language
from judge.ratings import rating_class
from judge.caching import cache_wrapper, CacheableModel

from typing import Optional


__all__ = ["Organization", "Profile", "OrganizationRequest", "Friend"]


TSHIRT_SIZES = (
    ("S", "Small (S)"),
    ("M", "Medium (M)"),
    ("L", "Large (L)"),
    ("XL", "Extra Large (XL)"),
    ("XXL", "2 Extra Large (XXL)"),
)


class EncryptedNullCharField(EncryptedCharField):
    def get_prep_value(self, value):
        if not value:
            return None
        return super(EncryptedNullCharField, self).get_prep_value(value)


def profile_image_path(profile, filename):
    tail = filename.split(".")[-1]
    new_filename = f"user_{profile.id}.{tail}"
    return os.path.join(settings.DMOJ_PROFILE_IMAGE_ROOT, new_filename)


def profile_background_path(profile_id, filename):
    tail = filename.split(".")[-1]
    new_filename = f"bg_user_{profile_id}.{tail}"
    return os.path.join(settings.DMOJ_PROFILE_IMAGE_ROOT, new_filename)


def organization_image_path(organization, filename):
    tail = filename.split(".")[-1]
    new_filename = f"organization_{organization.id}.{tail}"
    return os.path.join(settings.DMOJ_ORGANIZATION_IMAGE_ROOT, new_filename)


class Organization(CacheableModel):
    name = models.CharField(max_length=128, verbose_name=_("organization title"))
    slug = models.SlugField(
        max_length=128,
        verbose_name=_("organization slug"),
        help_text=_("Organization name shown in URL"),
        unique=True,
        validators=[
            RegexValidator("^[-a-zA-Z0-9]+$", _("Only alphanumeric and hyphens"))
        ],
    )
    short_name = models.CharField(
        max_length=20,
        verbose_name=_("short name"),
        help_text=_("Displayed beside user name during contests"),
    )
    about = models.CharField(
        max_length=10000, verbose_name=_("organization description")
    )
    registrant = models.ForeignKey(
        "Profile",
        verbose_name=_("registrant"),
        on_delete=models.CASCADE,
        related_name="registrant+",
        help_text=_("User who registered this organization"),
    )
    admins = models.ManyToManyField(
        "Profile",
        verbose_name=_("administrators"),
        related_name="admin_of",
        help_text=_("Those who can edit this organization"),
    )
    creation_date = models.DateTimeField(
        verbose_name=_("creation date"), auto_now_add=True
    )
    is_open = models.BooleanField(
        verbose_name=_("is open organization?"),
        help_text=_("Allow joining organization"),
        default=True,
    )
    slots = models.IntegerField(
        verbose_name=_("maximum size"),
        null=True,
        blank=True,
        help_text=_(
            "Maximum amount of users in this organization, "
            "only applicable to private organizations"
        ),
    )
    access_code = models.CharField(
        max_length=7,
        help_text=_("Student access code"),
        verbose_name=_("access code"),
        null=True,
        blank=True,
    )
    organization_image = models.ImageField(upload_to=organization_image_path, null=True)

    @classmethod
    def get_cached_dict(cls, org_id):
        return _get_organization(org_id)

    @classmethod
    def dirty_cache(cls, *ids):
        id_list = [(id,) for id in ids]
        _get_organization.dirty_multi(id_list)
        Organization.get_member_ids.dirty_multi(id_list)
        Organization.get_admin_ids.dirty_multi(id_list)

    def __contains__(self, item):
        if isinstance(item, int):
            return item in self.get_member_ids()
        elif isinstance(item, Profile):
            return item.id in self.get_member_ids()
        else:
            raise TypeError(
                "Organization membership test must be Profile or primany key"
            )

    def delete(self, *args, **kwargs):
        contests = self.contest_set
        for contest in contests.all():
            if contest.organizations.count() == 1:
                contest.delete()
        args_list = [(profile_id,) for profile_id in self.get_member_ids()]
        _get_most_recent_organization_ids.dirty_multi(args_list)
        Profile.get_organization_ids.dirty_multi(args_list)
        super().delete(*args, **kwargs)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("organization_home", args=(self.id, self.slug))

    def get_users_url(self):
        return reverse("organization_users", args=(self.id, self.slug))

    def get_problems_url(self):
        return reverse("organization_problems", args=(self.id, self.slug))

    def get_contests_url(self):
        return reverse("organization_contests", args=(self.id, self.slug))

    def get_submissions_url(self):
        return reverse("organization_submissions", args=(self.id, self.slug))

    def get_name(self):
        return self.get_cached_value("name")

    def get_slug(self):
        return self.get_cached_value("slug")

    def get_short_name(self):
        return self.get_cached_value("short_name")

    def get_organization_image_url(self):
        return self.get_cached_value("organization_image_url")

    @classmethod
    def get_cached_instances(cls, *ids):
        # Prefetch cache data
        _get_organization.batch([(id,) for id in ids])
        return [cls(id=id) for id in ids]

    def is_admin(self, profile):
        return profile.id in self.get_admin_ids()

    @cache_wrapper(prefix="Orgai", expected_type=list)
    def get_admin_ids(self):
        return list(self.admins.values_list("id", flat=True))

    @cache_wrapper(prefix="Orgmi", expected_type=list)
    def get_member_ids(self):
        return list(self.members.values_list("id", flat=True))

    def is_member(self, profile):
        return profile in self

    class Meta:
        ordering = ["name"]
        permissions = (
            ("organization_admin", "Administer organizations"),
            ("edit_all_organization", "Edit all organizations"),
        )
        verbose_name = _("organization")
        verbose_name_plural = _("organizations")
        app_label = "judge"


class Profile(CacheableModel):
    user = models.OneToOneField(
        User, verbose_name=_("user associated"), on_delete=models.CASCADE
    )
    about = models.CharField(
        max_length=10000, verbose_name=_("self-description"), null=True, blank=True
    )
    timezone = models.CharField(
        max_length=50,
        verbose_name=_("location"),
        choices=TIMEZONE,
        default=settings.DEFAULT_USER_TIME_ZONE,
    )
    language = models.ForeignKey(
        "Language",
        verbose_name=_("preferred language"),
        on_delete=models.SET_DEFAULT,
        default=Language.get_default_language_pk,
    )
    points = models.FloatField(default=0, db_index=True)
    performance_points = models.FloatField(default=0, db_index=True)
    problem_count = models.IntegerField(default=0, db_index=True)
    ace_theme = models.CharField(max_length=30, choices=ACE_THEMES, default="github")
    last_access = models.DateTimeField(
        verbose_name=_("last access time"), default=now, db_index=True
    )
    ip = models.GenericIPAddressField(verbose_name=_("last IP"), blank=True, null=True)
    organizations = SortedManyToManyField(
        Organization,
        verbose_name=_("organization"),
        blank=True,
        related_name="members",
        related_query_name="member",
    )
    display_rank = models.CharField(
        max_length=10,
        default="user",
        verbose_name=_("display rank"),
        choices=(
            ("user", "Normal User"),
            ("setter", "Problem Setter"),
            ("admin", "Admin"),
        ),
        db_index=True,
    )
    mute = models.BooleanField(
        verbose_name=_("comment mute"),
        help_text=_("Some users are at their best when silent."),
        default=False,
    )
    is_unlisted = models.BooleanField(
        verbose_name=_("unlisted user"),
        help_text=_("User will not be ranked."),
        default=False,
    )
    rating = models.IntegerField(null=True, default=None, db_index=True)
    current_contest = models.OneToOneField(
        "ContestParticipation",
        verbose_name=_("current contest"),
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    is_totp_enabled = models.BooleanField(
        verbose_name=_("2FA enabled"),
        default=False,
        help_text=_("check to enable TOTP-based two factor authentication"),
    )
    totp_key = EncryptedNullCharField(
        max_length=32,
        null=True,
        blank=True,
        verbose_name=_("TOTP key"),
        help_text=_("32 character base32-encoded key for TOTP"),
        validators=[
            RegexValidator("^$|^[A-Z2-7]{32}$", _("TOTP key must be empty or base32"))
        ],
    )
    notes = models.TextField(
        verbose_name=_("internal notes"),
        null=True,
        blank=True,
        help_text=_("Notes for administrators regarding this user."),
    )
    profile_image = models.ImageField(upload_to=profile_image_path, null=True)
    css_background = models.TextField(
        verbose_name=_("Custom background"),
        null=True,
        blank=True,
        help_text=_('CSS custom background properties: url("image_url"), color, etc'),
        max_length=300,
    )

    @classmethod
    def get_cached_dict(cls, profile_id):
        return _get_profile(profile_id)

    @classmethod
    def get_cached_instances(cls, *ids):
        # Prefetch cache data
        _get_profile.batch([(id,) for id in ids])
        return [cls(id=id) for id in ids]

    @classmethod
    def prefetch_cache_about(cls, *ids):
        """Prefetch the about field for multiple profiles"""
        _get_about.batch([(id,) for id in ids])

    @classmethod
    def dirty_cache(cls, *ids):
        id_list = [(id,) for id in ids]
        _get_profile.dirty_multi(id_list)
        _get_profile_last_access.dirty_multi(id_list)
        _get_about.dirty_multi(id_list)

    @cached_property
    def organization(self):
        # We do this to take advantage of prefetch_related
        orgs = self.organizations.all()
        return orgs[0] if orgs else None

    @cached_property
    def username(self):
        return self.get_username()

    @cached_property
    def first_name(self):
        return self.get_first_name()

    @cached_property
    def last_name(self):
        return self.get_last_name()

    @cached_property
    def email(self):
        return self.get_email()

    def get_username(self):
        return self.get_cached_value("username")

    def get_first_name(self):
        return self.get_cached_value("first_name")

    def get_last_name(self):
        return self.get_cached_value("last_name")

    def get_email(self):
        return self.get_cached_value("email")

    def get_mute(self):
        return self.get_cached_value("mute")

    def get_display_rank(self):
        return self.get_cached_value("display_rank")

    def get_rating(self):
        return self.get_cached_value("rating")

    def get_profile_image_url(self):
        return self.get_cached_value("profile_image_url")

    def get_about(self):
        return _get_about(self.id)

    def get_points(self):
        return self.get_cached_value("points")

    def get_problem_count(self):
        return self.get_cached_value("problem_count")

    def get_performance_points(self):
        return self.get_cached_value("performance_points")

    def get_last_access(self):
        return _get_profile_last_access(self.id)

    def get_num_unseen_notifications(self):
        from judge.models.notification import unseen_notifications_count

        return unseen_notifications_count(self)

    def get_num_unread_chat_boxes(self):
        from chat_box.utils import get_unread_boxes

        return get_unread_boxes(self)

    @cache_wrapper(prefix="Pgoi", expected_type=list)
    def get_organization_ids(self):
        return list(self.organizations.values_list("id", flat=True))

    @cache_wrapper(prefix="Pgoai", expected_type=list)
    def get_admin_organization_ids(self):
        return list(self.admin_of.values_list("id", flat=True))

    def get_organizations(self):
        organization_ids = self.get_organization_ids()
        return Organization.get_cached_instances(*organization_ids)

    _pp_table = [pow(settings.DMOJ_PP_STEP, i) for i in range(settings.DMOJ_PP_ENTRIES)]

    def calculate_points(self, table=_pp_table):
        from judge.models import Problem

        public_problems = Problem.get_public_problems()
        data = (
            public_problems.filter(
                submission__user=self, submission__points__isnull=False
            )
            .annotate(max_points=Max("submission__points"))
            .order_by("-max_points")
            .values_list("max_points", flat=True)
            .filter(max_points__gt=0)
        )
        extradata = (
            public_problems.filter(submission__user=self, submission__result="AC")
            .values("id")
            .distinct()
            .count()
        )
        bonus_function = settings.DMOJ_PP_BONUS_FUNCTION
        points = sum(data)
        problems = len(data)
        entries = min(len(data), len(table))
        pp = sum(map(mul, table[:entries], data[:entries])) + bonus_function(extradata)
        if (
            self.points != points
            or problems != self.problem_count
            or self.performance_points != pp
        ):
            self.points = points
            self.problem_count = problems
            self.performance_points = pp
            self.save(update_fields=["points", "problem_count", "performance_points"])
        return points

    calculate_points.alters_data = True

    def remove_contest(self):
        self.current_contest = None
        self.save()

    remove_contest.alters_data = True

    def update_contest(self):
        from judge.models import ContestParticipation

        try:
            contest = self.current_contest
            if contest is not None and (
                contest.ended or not contest.contest.is_accessible_by(self.user)
            ):
                self.remove_contest()
        except ContestParticipation.DoesNotExist:
            self.remove_contest()

    update_contest.alters_data = True

    def get_absolute_url(self):
        return reverse("user_page", args=(self.get_username(),))

    def __str__(self):
        return self.get_username()

    @classmethod
    def get_user_css_class(
        cls, display_rank, rating, rating_colors=settings.DMOJ_RATING_COLORS
    ):
        if rating_colors:
            return "rating %s %s" % (
                rating_class(rating) if rating is not None else "rate-none",
                display_rank,
            )
        return display_rank

    @cached_property
    def css_class(self):
        return self.get_user_css_class(self.get_display_rank(), self.get_rating())

    def get_following_ids(
        self, include_self=False
    ):  # list of ids of users who follow this user
        res = _get_following_ids(self.id)
        if include_self:
            res.append(self.id)
        return res

    def get_follower_ids(self):  # list of ids of users who follow this user
        return _get_follower_ids(self.id)

    def is_followed_by(self, profile):
        if not profile:
            return False
        return profile.id in self.get_follower_ids()

    def can_edit_organization(self, org):
        if not self.user.is_authenticated:
            return False
        profile_id = self.id
        return org.is_admin(self) or self.user.is_superuser

    class Meta:
        indexes = [
            models.Index(fields=["is_unlisted", "performance_points"]),
        ]
        permissions = (
            ("test_site", "Shows in-progress development stuff"),
            ("totp", "Edit TOTP settings"),
        )
        verbose_name = _("user profile")
        verbose_name_plural = _("user profiles")


class ProfileInfo(models.Model):
    profile = models.OneToOneField(
        Profile,
        verbose_name=_("profile associated"),
        on_delete=models.CASCADE,
        related_name="info",
    )
    tshirt_size = models.CharField(
        max_length=5,
        choices=TSHIRT_SIZES,
        verbose_name=_("t-shirt size"),
        null=True,
        blank=True,
    )
    date_of_birth = models.DateField(
        verbose_name=_("date of birth"),
        null=True,
        blank=True,
    )
    address = models.CharField(
        max_length=255,
        verbose_name=_("address"),
        null=True,
        blank=True,
    )

    def __str__(self):
        return f"{self.profile.get_username()}'s Info"


class OrganizationRequest(models.Model):
    user = models.ForeignKey(
        Profile,
        verbose_name=_("user"),
        related_name="requests",
        on_delete=models.CASCADE,
    )
    organization = models.ForeignKey(
        Organization,
        verbose_name=_("organization"),
        related_name="requests",
        on_delete=models.CASCADE,
    )
    time = models.DateTimeField(verbose_name=_("request time"), auto_now_add=True)
    state = models.CharField(
        max_length=1,
        verbose_name=_("state"),
        choices=(
            ("P", "Pending"),
            ("A", "Approved"),
            ("R", "Rejected"),
        ),
    )
    reason = models.TextField(verbose_name=_("reason"))

    class Meta:
        verbose_name = _("organization join request")
        verbose_name_plural = _("organization join requests")


class Friend(models.Model):
    users = models.ManyToManyField(Profile)
    current_user = models.ForeignKey(
        Profile,
        related_name="following_users",
        on_delete=CASCADE,
    )

    @classmethod
    def make_follow(cls, from_profile, to_profile):
        friend, created = cls.objects.get_or_create(current_user=from_profile)
        friend.users.add(to_profile)
        _get_follower_ids.dirty(to_profile.id)

    @classmethod
    def remove_follow(cls, from_profile, to_profile):
        friend, created = cls.objects.get_or_create(current_user=from_profile)
        friend.users.remove(to_profile)
        _get_follower_ids.dirty(to_profile.id)

    @classmethod
    def toggle_follow(cls, from_profile, to_profile):
        if to_profile.is_followed_by(from_profile):
            cls.remove_follow(from_profile, to_profile)
        else:
            cls.make_follow(from_profile, to_profile)

    def __str__(self):
        return str(self.current_user)


class OrganizationProfile(models.Model):
    profile = models.ForeignKey(
        Profile,
        verbose_name=_("user"),
        related_name="last_visit",
        on_delete=models.CASCADE,
        db_index=True,
    )
    organization = models.ForeignKey(
        Organization,
        verbose_name=_("organization"),
        related_name="last_vist",
        on_delete=models.CASCADE,
    )
    last_visit_time = models.DateTimeField(
        verbose_name=_("last visit"),
        default=now,
        db_index=True,
    )

    @classmethod
    def add_organization(cls, profile, organization):
        orgs = _get_most_recent_organization_ids(profile)
        if orgs and orgs[0] == organization.id:
            return
        obj, created = cls.objects.update_or_create(
            profile=profile,
            organization=organization,
            defaults={"last_visit_time": now()},
        )
        _get_most_recent_organization_ids.dirty(profile)

    @classmethod
    def get_most_recent_organizations(cls, profile):
        org_ids = _get_most_recent_organization_ids(profile)
        return Organization.get_cached_instances(*org_ids)


@cache_wrapper("OPgmroid", expected_type=list)
def _get_most_recent_organization_ids(profile):
    return list(
        OrganizationProfile.objects.filter(profile=profile)
        .order_by("-last_visit_time")
        .values_list("organization_id", flat=True)[:5]
    )


@receiver([post_save], sender=User)
def on_user_save(sender, instance, **kwargs):
    try:
        profile = instance.profile
        _get_profile.dirty(profile.id)
    except:
        pass


@receiver(m2m_changed, sender=Profile.organizations.through)
def on_profile_organization_change(sender, instance, action, **kwargs):
    if action in ["post_add", "post_remove", "post_clear"]:
        if isinstance(instance, Profile):
            Profile.get_organization_ids.dirty(instance)

            # Also invalidate the organization's member cache
            org_pk_set = kwargs.get("pk_set")
            if org_pk_set:
                for org_id in org_pk_set:
                    Organization.get_member_ids.dirty(org_id)


@receiver(m2m_changed, sender=Organization.admins.through)
def on_organization_admin_change(sender, instance, action, **kwargs):
    if action in ["post_add", "post_remove", "post_clear"]:
        if isinstance(instance, Organization):
            Organization.get_admin_ids.dirty(instance)

            # Also invalidate the admin_of cache for each affected profile
            profile_pk_set = kwargs.get("pk_set")
            if profile_pk_set:
                for profile_id in profile_pk_set:
                    Profile.get_admin_organization_ids.dirty(profile_id)


def _get_profile_batch(args_list):
    # args_list = [(profile_id1, ), (profile_id2, ), ...]
    profile_ids = [args[0] for args in args_list]

    profiles = (
        Profile.objects.filter(id__in=profile_ids).select_related("user").defer("about")
    )

    profile_dict = {}
    for profile in profiles:
        profile_id = profile.id
        profile_dict[profile_id] = {
            "email": profile.user.email,
            "username": profile.user.username,
            "mute": profile.mute,
            "first_name": profile.user.first_name or None,
            "last_name": profile.user.last_name or None,
            "profile_image_url": (
                profile.profile_image.url if profile.profile_image else None
            ),
            "display_rank": profile.display_rank,
            "rating": profile.rating,
            "points": profile.points,
            "problem_count": profile.problem_count,
            "performance_points": profile.performance_points,
        }
        # Remove None values to save cache space
        profile_dict[profile_id] = {
            k: v for k, v in profile_dict[profile_id].items() if v is not None
        }

    results = []
    for profile_id in profile_ids:
        if profile_id in profile_dict:
            results.append(profile_dict[profile_id])
        else:
            assert False, f"Invalid profile_id, {profile_id}"

    return results


@cache_wrapper(prefix="Pgbi5", expected_type=dict, batch_fn=_get_profile_batch)
def _get_profile(profile_id):
    results = _get_profile_batch([(profile_id,)])
    return results[0]


@cache_wrapper(prefix="Pgla", expected_type=datetime)
def _get_profile_last_access(profile_id):
    return Profile.objects.values_list("last_access", flat=True).get(id=profile_id)


def _get_about_batch(args_list):
    # args_list = [(profile_id1, ), (profile_id2, ), ...]
    profile_ids = [args[0] for args in args_list]

    abouts = {}
    for profile in Profile.objects.filter(id__in=profile_ids).values("id", "about"):
        abouts[profile["id"]] = profile["about"] or ""

    results = []
    for profile_id in profile_ids:
        if profile_id in abouts:
            results.append(abouts[profile_id])
        else:
            assert False, f"Invalid profile_id, {profile_id}"

    return results


@cache_wrapper(prefix="Pgab", expected_type=str, batch_fn=_get_about_batch)
def _get_about(profile_id):
    results = _get_about_batch([(profile_id,)])
    return results[0]


@cache_wrapper(prefix="Pgtrpi", timeout=1800, expected_type=list)
def _get_top_rating_profile_inner(organization_id):
    qs = (
        Profile.objects.filter(is_unlisted=False)
        .order_by("-rating")
        .values_list("id", flat=True)
    )
    if organization_id is not None:
        qs = qs.filter(organizations=organization_id)
    return list(qs[:10])


def get_top_rating_profile(organization_id=None):
    profile_ids = _get_top_rating_profile_inner(organization_id)
    return Profile.get_cached_instances(*profile_ids)


@cache_wrapper(prefix="Pgtspi", timeout=1800, expected_type=list)
def _get_top_score_profile_inner(organization_id=None):
    qs = (
        Profile.objects.filter(is_unlisted=False)
        .order_by("-performance_points")
        .values_list("id", flat=True)
    )
    if organization_id is not None:
        qs = qs.filter(organizations=organization_id)
    return list(qs[:10])


def get_top_score_profile(organization_id=None):
    profile_ids = _get_top_score_profile_inner(organization_id)
    return Profile.get_cached_instances(*profile_ids)


def _get_organization_batch(args_list):
    # args_list = [(org_id1, ), (org_id2, ), ...]
    org_ids = [args[0] for args in args_list]

    organizations = Organization.objects.filter(id__in=org_ids).only(
        "name", "slug", "short_name", "organization_image"
    )

    org_dict = {}
    for org in organizations:
        org_id = org.id
        org_dict[org_id] = {
            "name": org.name,
            "slug": org.slug,
            "short_name": org.short_name,
            "organization_image_url": (
                org.organization_image.url if org.organization_image else None
            ),
        }
        org_dict[org_id] = {k: v for k, v in org_dict[org_id].items() if v is not None}

    results = []
    for org_id in org_ids:
        if org_id in org_dict:
            results.append(org_dict[org_id])
        else:
            assert False, f"Invalid organization_id, {org_id}"

    return results


@cache_wrapper(prefix="Ogoi", expected_type=dict, batch_fn=_get_organization_batch)
def _get_organization(org_id):
    results = _get_organization_batch([(org_id,)])
    return results[0]


def _get_profile_id_from_username_batch(args_list):
    # args_list = [(username1, ), (username2, ), ...]
    usernames = [args[0] for args in args_list]

    # Get profile id and username mappings
    username_to_id = dict(
        Profile.objects.filter(user__username__in=usernames).values_list(
            "user__username", "id"
        )
    )

    # Return results in the same order as input usernames
    results = []
    for username in usernames:
        if username in username_to_id:
            results.append(username_to_id[username])
        else:
            results.append(None)

    return results


@cache_wrapper(
    prefix="gpifu",
    expected_type=Optional[int],
    batch_fn=_get_profile_id_from_username_batch,
)
def get_profile_id_from_username(username):
    results = _get_profile_id_from_username_batch([(username,)])
    return results[0]


@cache_wrapper(prefix="Pgfids", expected_type=list)
def _get_follower_ids(profile_id):
    """Get a list of profile IDs that follow the given profile"""
    followers = []
    for friend in Friend.objects.filter(users__id=profile_id).select_related(
        "current_user"
    ):
        followers.append(friend.current_user.id)
    return followers


@cache_wrapper(prefix="Pgfgids", expected_type=list)
def _get_following_ids(profile_id):
    """Get a list of profile IDs that given profile is following"""
    return list(
        Friend.objects.filter(current_user=profile_id).values_list(
            "users__id", flat=True
        )
    )
