import os
import secrets
from operator import attrgetter
import pyotp
import time
import datetime

from django import forms
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.core.validators import RegexValidator
from django.db.models import Q
from django.forms import (
    CharField,
    ChoiceField,
    Form,
    ModelForm,
    formset_factory,
    BaseModelFormSet,
    FileField,
)
from django.urls import reverse_lazy, reverse
from django.utils.translation import gettext_lazy as _
from django.utils import timezone

from django_ace import AceWidget
from judge.models import (
    Contest,
    Language,
    TestFormatterModel,
    Organization,
    PrivateMessage,
    Problem,
    ProblemPointsVote,
    Profile,
    Submission,
    BlogPost,
    ContestProblem,
    TestFormatterModel,
    ProfileInfo,
    Block,
    Course,
)

from judge.widgets import (
    HeavyPreviewPageDownWidget,
    PagedownWidget,
    Select2MultipleWidget,
    Select2Widget,
    HeavySelect2MultipleWidget,
    HeavySelect2Widget,
    Select2MultipleWidget,
    DateTimePickerWidget,
    ImageWidget,
    DatePickerWidget,
)


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


class ProfileInfoForm(ModelForm):
    class Meta:
        model = ProfileInfo
        fields = ["tshirt_size", "date_of_birth", "address"]
        widgets = {
            "tshirt_size": Select2Widget(attrs={"style": "width:100%"}),
            "date_of_birth": DatePickerWidget,
            "address": forms.TextInput(attrs={"style": "width:100%"}),
        }


class ProfileForm(ModelForm):
    background_image_upload = forms.ImageField(
        required=False, label=_("Background Image")
    )

    class Meta:
        model = Profile
        fields = [
            "about",
            "timezone",
            "language",
            "ace_theme",
            "profile_image",
            "css_background",
        ]
        widgets = {
            "timezone": Select2Widget(attrs={"style": "width:200px"}),
            "language": Select2Widget(attrs={"style": "width:200px"}),
            "ace_theme": Select2Widget(attrs={"style": "width:200px"}),
            "profile_image": ImageWidget,
            "css_background": forms.TextInput(),
        }

        if HeavyPreviewPageDownWidget is not None:
            widgets["about"] = HeavyPreviewPageDownWidget(
                preview=reverse_lazy("profile_preview"),
                attrs={"style": "max-width:700px;min-width:700px;width:700px"},
            )

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super(ProfileForm, self).__init__(*args, **kwargs)
        self.fields["profile_image"].required = False
        self.fields["background_image_upload"].widget = ImageWidget()

    def clean_profile_image(self):
        profile_image = self.cleaned_data.get("profile_image")
        if profile_image:
            if profile_image.size > 5 * 1024 * 1024:
                raise ValidationError(
                    _("File size exceeds the maximum allowed limit of 5MB.")
                )
        return profile_image

    def clean_background_image_upload(self):
        background_image = self.cleaned_data.get("background_image_upload")
        if background_image:
            if background_image.size > 10 * 1024 * 1024:
                raise ValidationError(
                    _("File size exceeds the maximum allowed limit of 10MB.")
                )
        return background_image


def file_size_validator(file):
    limit = 10 * 1024 * 1024
    if file.size > limit:
        raise ValidationError("File too large. Size should not exceed 10MB.")


class ProblemSubmitForm(ModelForm):
    source = CharField(
        max_length=65536, widget=AceWidget(theme="twilight", no_ace_media=True)
    )
    judge = ChoiceField(choices=(), widget=forms.HiddenInput(), required=False)
    source_file = FileField(required=False, validators=[file_size_validator])

    def __init__(self, *args, judge_choices=(), request=None, problem=None, **kwargs):
        super(ProblemSubmitForm, self).__init__(*args, **kwargs)
        self.source_file_name = None
        self.request = request
        self.problem = problem
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

    def allow_url_as_source(self):
        key = self.cleaned_data["language"].key
        filename = self.files["source_file"].name
        if key == "OUTPUT" and self.problem.data_files.output_only:
            return filename.endswith(".zip")
        if key == "SCAT":
            return filename.endswith(".sb3")
        return False

    def clean(self):
        if "source_file" in self.files:
            if self.allow_url_as_source():
                filename = self.files["source_file"].name
                now = datetime.datetime.now()
                timestamp = str(int(time.mktime(now.timetuple())))
                self.source_file_name = (
                    timestamp + secrets.token_hex(5) + "." + filename.split(".")[-1]
                )
                filepath = os.path.join(
                    settings.DMOJ_SUBMISSION_ROOT, self.source_file_name
                )
                with open(filepath, "wb+") as destination:
                    for chunk in self.files["source_file"].chunks():
                        destination.write(chunk)
                self.cleaned_data["source"] = self.request.build_absolute_uri(
                    reverse("submission_source_file", args=(self.source_file_name,))
                )
            del self.files["source_file"]
        return self.cleaned_data

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
            "organization_image",
            "admins",
            "is_open",
        ]
        widgets = {
            "admins": HeavySelect2MultipleWidget(data_view="profile_select2"),
            "organization_image": ImageWidget,
        }
        if HeavyPreviewPageDownWidget is not None:
            widgets["about"] = HeavyPreviewPageDownWidget(
                preview=reverse_lazy("organization_preview")
            )

    def __init__(self, *args, **kwargs):
        self.org_id = kwargs.pop("org_id", 0)
        super(EditOrganizationForm, self).__init__(*args, **kwargs)
        self.fields["organization_image"].required = False
        for field in [
            "admins",
        ]:
            self.fields[field].widget.data_url = (
                self.fields[field].widget.get_url() + f"?org_id={self.org_id}"
            )

    def clean_organization_image(self):
        organization_image = self.cleaned_data.get("organization_image")
        if organization_image:
            if organization_image.size > 5 * 1024 * 1024:
                raise ValidationError(
                    _("File size exceeds the maximum allowed limit of 5MB.")
                )
        return organization_image


class AddOrganizationForm(ModelForm):
    class Meta:
        model = Organization
        fields = [
            "name",
            "slug",
            "short_name",
            "about",
            "organization_image",
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
        self.fields["organization_image"].required = False

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
                self.fields[field].widget.get_url() + f"?org_id={self.org_id}"
            )

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
            "freeze_after",
            "use_clarifications",
            "hide_problem_tags",
            "public_scoreboard",
            "scoreboard_visibility",
            "points_precision",
            "rate_limit",
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

    def __init__(self, *args, **kwargs):
        self.organization = kwargs.pop("organization", None)
        if not self.organization:
            raise ValueError("An organization instance must be provided.")
        super().__init__(*args, **kwargs)

    def clean_new_users(self):
        new_users = self.cleaned_data.get("new_users") or ""
        usernames = new_users.split()
        non_existent_usernames = []
        blocked_usernames = []
        valid_profiles = []

        for username in usernames:
            profile = Profile.objects.filter(user__username=username).first()

            if not profile:
                non_existent_usernames.append(username)
            elif Block.is_blocked(blocker=profile, blocked=self.organization):
                blocked_usernames.append(username)
            else:
                valid_profiles.append(profile)

        if non_existent_usernames or blocked_usernames:
            error_messages = []
            if non_existent_usernames:
                error_messages.append(
                    _("These usernames don't exist: {usernames}").format(
                        usernames=", ".join(non_existent_usernames)
                    )
                )
            if blocked_usernames:
                error_messages.append(
                    _("These users have blocked this group: {usernames}").format(
                        usernames=", ".join(blocked_usernames)
                    )
                )
            raise ValidationError(error_messages)

        return valid_profiles

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
            widgets["content"] = PagedownWidget()


class CustomAuthenticationForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super(CustomAuthenticationForm, self).__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update(
            {"placeholder": _("Username/Email")}
        )
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

    target_type = forms.ChoiceField(
        choices=(),
        widget=forms.RadioSelect,
        required=True,
    )

    organization = forms.ChoiceField(
        choices=(),
        required=False,
        widget=Select2Widget(
            attrs={"class": "organization-field hidden-field", "style": "width: 100%"}
        ),
    )
    course = forms.ChoiceField(
        choices=(),
        required=False,
        widget=Select2Widget(
            attrs={"class": "course-field hidden-field", "style": "width: 100%"}
        ),
    )

    def __init__(
        self, *args, org_choices=(), course_choices=(), profile=None, **kwargs
    ):
        super(ContestCloneForm, self).__init__(*args, **kwargs)

        self.fields["organization"].choices = org_choices
        self.fields["organization"].widget.attrs.update(
            {
                "data-placeholder": _("Select a group"),
            }
        )

        self.fields["course"].choices = course_choices
        self.fields["course"].widget.attrs.update(
            {
                "data-placeholder": _("Select a course"),
            }
        )

        target_choices = []
        if org_choices:
            target_choices.append(("organization", _("Group")))
        if course_choices:
            target_choices.append(("course", _("Course")))

        self.fields["target_type"].choices = target_choices

        self.profile = profile

    def clean_key(self):
        key = self.cleaned_data["key"]
        if Contest.objects.filter(key=key).exists():
            raise ValidationError(_("Contest with key already exists."))
        return key

    def clean(self):
        cleaned_data = super().clean()
        target_type = cleaned_data.get("target_type")
        organization_id = cleaned_data.get("organization")
        course_id = cleaned_data.get("course")

        if target_type == "organization":
            if not organization_id:
                raise ValidationError(_("You must select a group."))
            try:
                organization = Organization.objects.get(id=organization_id)
            except Organization.DoesNotExist:
                raise ValidationError(_("Selected group doesn't exist."))
            if not organization.admins.filter(id=self.profile.id).exists():
                raise ValidationError(_("You don't have permission in this group."))
            cleaned_data["organization"] = organization

        elif target_type == "course":
            if not course_id:
                raise ValidationError(_("You must select a course."))
            try:
                course = Course.objects.get(id=course_id)
            except Course.DoesNotExist:
                raise ValidationError(_("Selected course doesn't exist."))
            if not Course.is_editable_by(course, self.profile):
                raise ValidationError(_("You don't have permission in this course."))
            cleaned_data["course"] = course

        else:
            raise ValidationError(_("Invalid target type selected."))

        return cleaned_data


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
            "show_testcases",
            "max_submissions",
        )
        widgets = {
            "problem": HeavySelect2Widget(
                data_view="problem_select2", attrs={"style": "width: 100%"}
            ),
        }


class ContestProblemModelFormSet(BaseModelFormSet):
    def is_valid(self):
        valid = super().is_valid()

        if not valid:
            return valid

        problems = set()
        duplicates = []

        for form in self.forms:
            if form.cleaned_data and not form.cleaned_data.get("DELETE", False):
                problem = form.cleaned_data.get("problem")
                if problem in problems:
                    duplicates.append(problem)
                else:
                    problems.add(problem)

        if duplicates:
            for form in self.forms:
                problem = form.cleaned_data.get("problem")
                if problem in duplicates:
                    form.add_error("problem", _("This problem is duplicated."))
            return False

        return True


class ContestProblemFormSet(
    formset_factory(
        ContestProblemForm, formset=ContestProblemModelFormSet, extra=6, can_delete=True
    )
):
    model = ContestProblem


class TestFormatterForm(ModelForm):
    class Meta:
        model = TestFormatterModel
        fields = ["file"]


class LessonCloneForm(Form):
    title = CharField(max_length=200, label=_("Lesson Title"))

    course = forms.CharField(
        max_length=100,
        widget=HeavySelect2Widget(
            data_view="course_select2", attrs={"style": "width: 100%"}
        ),
        label=_("Target Course"),
    )

    def __init__(self, *args, profile=None, **kwargs):
        super(LessonCloneForm, self).__init__(*args, **kwargs)

        self.fields["course"].widget.attrs.update(
            {
                "data-placeholder": _("Search for a course"),
            }
        )
        self.profile = profile

    def clean_course(self):
        course_slug = self.cleaned_data["course"]
        try:
            course = Course.objects.get(slug=course_slug)
            # Check if user can edit the target course
            if not Course.is_editable_by(course, self.profile):
                raise ValidationError(
                    _("You don't have permission to edit this course.")
                )
            return course
        except Course.DoesNotExist:
            raise ValidationError(_("Selected course does not exist."))
