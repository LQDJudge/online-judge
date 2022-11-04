from operator import attrgetter

import pyotp
from django import forms
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.core.validators import RegexValidator
from django.db import transaction
from django.db.models import Q
from django.forms import (
    CharField,
    ChoiceField,
    Form,
    ModelForm,
    formset_factory,
    BaseModelFormSet,
)
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.utils import timezone

from django_ace import AceWidget
from judge.models import (
    Contest,
    Language,
    Organization,
    PrivateMessage,
    Problem,
    ProblemPointsVote,
    Profile,
    Submission,
    BlogPost,
    ContestProblem,
)
from judge.utils.subscription import newsletter_id
from judge.widgets import (
    HeavyPreviewPageDownWidget,
    MathJaxPagedownWidget,
    PagedownWidget,
    Select2MultipleWidget,
    Select2Widget,
    HeavySelect2MultipleWidget,
    HeavySelect2Widget,
    Select2MultipleWidget,
    DateTimePickerWidget,
)
from judge.tasks import rescore_contest


def fix_unicode(string, unsafe=tuple("\u202a\u202b\u202d\u202e")):
    return (
        string + (sum(k in unsafe for k in string) - string.count("\u202c")) * "\u202c"
    )


class UserForm(ModelForm):
    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
        ]


class ProfileForm(ModelForm):
    if newsletter_id is not None:
        newsletter = forms.BooleanField(
            label=_("Subscribe to contest updates"), initial=False, required=False
        )
    test_site = forms.BooleanField(
        label=_("Enable experimental features"), initial=False, required=False
    )

    class Meta:
        model = Profile
        fields = [
            "about",
            "organizations",
            "timezone",
            "language",
            "ace_theme",
            "user_script",
        ]
        widgets = {
            "user_script": AceWidget(theme="github"),
            "timezone": Select2Widget(attrs={"style": "width:200px"}),
            "language": Select2Widget(attrs={"style": "width:200px"}),
            "ace_theme": Select2Widget(attrs={"style": "width:200px"}),
        }

        has_math_config = bool(settings.MATHOID_URL)
        if has_math_config:
            fields.append("math_engine")
            widgets["math_engine"] = Select2Widget(attrs={"style": "width:200px"})

        if HeavyPreviewPageDownWidget is not None:
            widgets["about"] = HeavyPreviewPageDownWidget(
                preview=reverse_lazy("profile_preview"),
                attrs={"style": "max-width:700px;min-width:700px;width:700px"},
            )

    def clean(self):
        organizations = self.cleaned_data.get("organizations") or []
        max_orgs = settings.DMOJ_USER_MAX_ORGANIZATION_COUNT

        if sum(org.is_open for org in organizations) > max_orgs:
            raise ValidationError(
                _("You may not be part of more than {count} public groups.").format(
                    count=max_orgs
                )
            )

        return self.cleaned_data

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super(ProfileForm, self).__init__(*args, **kwargs)
        if not user.has_perm("judge.edit_all_organization"):
            self.fields["organizations"].queryset = Organization.objects.filter(
                Q(is_open=True) | Q(id__in=user.profile.organizations.all()),
            )


class ProblemSubmitForm(ModelForm):
    source = CharField(
        max_length=65536, widget=AceWidget(theme="twilight", no_ace_media=True)
    )
    judge = ChoiceField(choices=(), widget=forms.HiddenInput(), required=False)

    def __init__(self, *args, judge_choices=(), **kwargs):
        super(ProblemSubmitForm, self).__init__(*args, **kwargs)
        self.fields["language"].empty_label = None
        self.fields["language"].label_from_instance = attrgetter("display_name")
        self.fields["language"].queryset = Language.objects.filter(
            judges__online=True
        ).distinct()

        if judge_choices:
            self.fields["judge"].widget = Select2Widget(
                attrs={"style": "width: 150px", "data-placeholder": _("Any judge")},
            )
            self.fields["judge"].choices = judge_choices

    class Meta:
        model = Submission
        fields = ["language"]


class EditOrganizationForm(ModelForm):
    class Meta:
        model = Organization
        fields = [
            "name",
            "slug",
            "short_name",
            "about",
            "logo_override_image",
            "admins",
            "is_open",
        ]
        widgets = {"admins": Select2MultipleWidget()}
        if HeavyPreviewPageDownWidget is not None:
            widgets["about"] = HeavyPreviewPageDownWidget(
                preview=reverse_lazy("organization_preview")
            )


class AddOrganizationForm(ModelForm):
    class Meta:
        model = Organization
        fields = [
            "name",
            "slug",
            "short_name",
            "about",
            "logo_override_image",
            "is_open",
        ]
        widgets = {}
        if HeavyPreviewPageDownWidget is not None:
            widgets["about"] = HeavyPreviewPageDownWidget(
                preview=reverse_lazy("organization_preview")
            )

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super(AddOrganizationForm, self).__init__(*args, **kwargs)

    def save(self, commit=True):
        res = super(AddOrganizationForm, self).save(commit=False)
        res.registrant = self.request.profile
        if commit:
            res.save()
        return res


class AddOrganizationContestForm(ModelForm):
    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super(AddOrganizationContestForm, self).__init__(*args, **kwargs)

    def save(self, commit=True):
        contest = super(AddOrganizationContestForm, self).save(commit=False)
        old_save_m2m = self.save_m2m

        def save_m2m():
            for i, problem in enumerate(self.cleaned_data["problems"]):
                contest_problem = ContestProblem(
                    contest=contest, problem=problem, points=100, order=i + 1
                )
                contest_problem.save()
                contest.contest_problems.add(contest_problem)
            old_save_m2m()

        self.save_m2m = save_m2m
        contest.save()
        self.save_m2m()
        return contest

    class Meta:
        model = Contest
        fields = (
            "key",
            "name",
            "start_time",
            "end_time",
            "problems",
        )
        widgets = {
            "start_time": DateTimePickerWidget(),
            "end_time": DateTimePickerWidget(),
            "problems": HeavySelect2MultipleWidget(data_view="problem_select2"),
        }


class EditOrganizationContestForm(ModelForm):
    def __init__(self, *args, **kwargs):
        self.org_id = kwargs.pop("org_id", 0)
        super(EditOrganizationContestForm, self).__init__(*args, **kwargs)
        for field in [
            "authors",
            "curators",
            "testers",
            "private_contestants",
            "banned_users",
            "view_contest_scoreboard",
        ]:
            self.fields[field].widget.data_url = (
                self.fields[field].widget.get_url() + "?org_id=1"
            )

    def save(self, commit=True):
        res = super(EditOrganizationContestForm, self).save(commit=False)
        if commit:
            res.save()
            transaction.on_commit(rescore_contest.s(res.key).delay)
        return res

    class Meta:
        model = Contest
        fields = (
            "is_visible",
            "key",
            "name",
            "start_time",
            "end_time",
            "format_name",
            "authors",
            "curators",
            "testers",
            "time_limit",
            "use_clarifications",
            "hide_problem_tags",
            "scoreboard_visibility",
            "run_pretests_only",
            "points_precision",
            "description",
            "og_image",
            "logo_override_image",
            "summary",
            "access_code",
            "private_contestants",
            "view_contest_scoreboard",
            "banned_users",
        )
        widgets = {
            "authors": HeavySelect2MultipleWidget(data_view="profile_select2"),
            "curators": HeavySelect2MultipleWidget(data_view="profile_select2"),
            "testers": HeavySelect2MultipleWidget(data_view="profile_select2"),
            "private_contestants": HeavySelect2MultipleWidget(
                data_view="profile_select2"
            ),
            "banned_users": HeavySelect2MultipleWidget(data_view="profile_select2"),
            "view_contest_scoreboard": HeavySelect2MultipleWidget(
                data_view="profile_select2"
            ),
            "organizations": HeavySelect2MultipleWidget(
                data_view="organization_select2"
            ),
            "tags": Select2MultipleWidget,
            "description": HeavyPreviewPageDownWidget(
                preview=reverse_lazy("contest_preview")
            ),
            "start_time": DateTimePickerWidget(),
            "end_time": DateTimePickerWidget(),
            "format_name": Select2Widget(),
            "scoreboard_visibility": Select2Widget(),
        }


class AddOrganizationMemberForm(ModelForm):
    new_users = CharField(
        max_length=65536,
        widget=forms.Textarea,
        help_text=_("Enter usernames separating by space"),
        label=_("New users"),
    )

    def clean(self):
        new_users = self.cleaned_data.get("new_users") or ""
        usernames = new_users.split()
        invalid_usernames = []
        valid_usernames = []

        for username in usernames:
            try:
                valid_usernames.append(Profile.objects.get(user__username=username))
            except ObjectDoesNotExist:
                invalid_usernames.append(username)

        if invalid_usernames:
            raise ValidationError(
                _("These usernames don't exist: {usernames}").format(
                    usernames=str(invalid_usernames)
                )
            )
        self.cleaned_data["new_users"] = valid_usernames
        return self.cleaned_data

    class Meta:
        model = Organization
        fields = ()


class OrganizationBlogForm(ModelForm):
    class Meta:
        model = BlogPost
        fields = ("title", "content", "publish_on")
        widgets = {
            "publish_on": forms.HiddenInput,
        }
        if HeavyPreviewPageDownWidget is not None:
            widgets["content"] = HeavyPreviewPageDownWidget(
                preview=reverse_lazy("organization_preview")
            )

    def __init__(self, *args, **kwargs):
        super(OrganizationBlogForm, self).__init__(*args, **kwargs)
        self.fields["publish_on"].required = False
        self.fields["publish_on"].is_hidden = True

    def clean(self):
        self.cleaned_data["publish_on"] = timezone.now()
        return self.cleaned_data


class OrganizationAdminBlogForm(OrganizationBlogForm):
    class Meta:
        model = BlogPost
        fields = ("visible", "sticky", "title", "content", "publish_on")
        widgets = {
            "publish_on": forms.HiddenInput,
        }
        if HeavyPreviewPageDownWidget is not None:
            widgets["content"] = HeavyPreviewPageDownWidget(
                preview=reverse_lazy("organization_preview")
            )


class NewMessageForm(ModelForm):
    class Meta:
        model = PrivateMessage
        fields = ["title", "content"]
        widgets = {}
        if PagedownWidget is not None:
            widgets["content"] = MathJaxPagedownWidget()


class CustomAuthenticationForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super(CustomAuthenticationForm, self).__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"placeholder": _("Username")})
        self.fields["password"].widget.attrs.update({"placeholder": _("Password")})

        self.has_google_auth = self._has_social_auth("GOOGLE_OAUTH2")
        self.has_facebook_auth = self._has_social_auth("FACEBOOK")
        self.has_github_auth = self._has_social_auth("GITHUB_SECURE")

    def _has_social_auth(self, key):
        return getattr(settings, "SOCIAL_AUTH_%s_KEY" % key, None) and getattr(
            settings, "SOCIAL_AUTH_%s_SECRET" % key, None
        )


class NoAutoCompleteCharField(forms.CharField):
    def widget_attrs(self, widget):
        attrs = super(NoAutoCompleteCharField, self).widget_attrs(widget)
        attrs["autocomplete"] = "off"
        return attrs


class TOTPForm(Form):
    TOLERANCE = settings.DMOJ_TOTP_TOLERANCE_HALF_MINUTES

    totp_token = NoAutoCompleteCharField(
        validators=[
            RegexValidator(
                "^[0-9]{6}$",
                _("Two Factor Authentication tokens must be 6 decimal digits."),
            ),
        ]
    )

    def __init__(self, *args, **kwargs):
        self.totp_key = kwargs.pop("totp_key")
        super(TOTPForm, self).__init__(*args, **kwargs)

    def clean_totp_token(self):
        if not pyotp.TOTP(self.totp_key).verify(
            self.cleaned_data["totp_token"], valid_window=self.TOLERANCE
        ):
            raise ValidationError(_("Invalid Two Factor Authentication token."))


class ProblemCloneForm(Form):
    code = CharField(
        max_length=20,
        validators=[
            RegexValidator("^[a-z0-9]+$", _("Problem code must be ^[a-z0-9]+$"))
        ],
    )

    def clean_code(self):
        code = self.cleaned_data["code"]
        if Problem.objects.filter(code=code).exists():
            raise ValidationError(_("Problem with code already exists."))
        return code


class ContestCloneForm(Form):
    key = CharField(
        max_length=20,
        validators=[RegexValidator("^[a-z0-9]+$", _("Contest id must be ^[a-z0-9]+$"))],
    )

    def clean_key(self):
        key = self.cleaned_data["key"]
        if Contest.objects.filter(key=key).exists():
            raise ValidationError(_("Contest with key already exists."))
        return key


class ProblemPointsVoteForm(ModelForm):
    class Meta:
        model = ProblemPointsVote
        fields = ["points"]


class ContestProblemForm(ModelForm):
    class Meta:
        model = ContestProblem
        fields = (
            "order",
            "problem",
            "points",
            "partial",
            "output_prefix_override",
            "max_submissions",
        )
        widgets = {
            "problem": HeavySelect2Widget(
                data_view="problem_select2", attrs={"style": "width:100%"}
            ),
        }


class ContestProblemFormSet(
    formset_factory(
        ContestProblemForm, formset=BaseModelFormSet, extra=6, can_delete=True
    )
):
    model = ContestProblem
