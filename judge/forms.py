import secrets
from operator import attrgetter
import pyotp
import time
import datetime

from django import forms
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.validators import RegexValidator
from django.forms import (
    CharField,
    ChoiceField,
    Form,
    ModelForm,
    formset_factory,
    BaseModelFormSet,
    FileField,
)
from django.core.files.uploadedfile import UploadedFile
from django.utils.html import format_html
from django.forms.utils import flatatt
from django.urls import reverse_lazy, reverse
from django.utils.translation import gettext_lazy as _
from django.utils import timezone

from django_ace import AceWidget
from judge.models import (
    Contest,
    Language,
    LanguageLimit,
    LanguageTemplate,
    Organization,
    PrivateMessage,
    Problem,
    ProblemPointsVote,
    ProblemTranslation,
    Profile,
    Solution,
    Submission,
    BlogPost,
    ContestProblem,
    ProfileInfo,
    Block,
    Course,
)
from judge import contest_format

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
    PDFWidget,
)


class HTMLDisplayWidget(forms.Widget):
    """Widget that displays HTML content without escaping"""

    def __init__(self, attrs=None):
        default_attrs = {"readonly": "readonly", "style": "width: 100%"}
        if attrs:
            default_attrs.update(attrs)
        super().__init__(default_attrs)

    def render(self, name, value, attrs=None, renderer=None):
        if value is None:
            value = ""

        final_attrs = self.build_attrs(attrs, {"name": name})

        # Create a div that displays the HTML content
        return format_html(
            '<div{} style="padding: 8px; border: 1px solid #ccc; background-color: #f9f9f9; border-radius: 4px;">{}</div>',
            flatatt(final_attrs),
            value,
        )

    def value_from_datadict(self, data, files, name):
        # This widget is read-only, so return None
        return None


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
    class Meta:
        model = Profile
        fields = [
            "about",
            "timezone",
            "language",
            "ace_theme",
            "profile_image",
            "background_image",
        ]
        widgets = {
            "timezone": Select2Widget(attrs={"style": "width:200px"}),
            "language": Select2Widget(attrs={"style": "width:200px"}),
            "ace_theme": Select2Widget(attrs={"style": "width:200px"}),
            "profile_image": ImageWidget,
            "background_image": ImageWidget,
        }

        if HeavyPreviewPageDownWidget is not None:
            widgets["about"] = HeavyPreviewPageDownWidget(
                preview=reverse_lazy("profile_preview"),
                attrs={"style": "max-width:700px;min-width:700px;width:700px"},
            )

    def __init__(self, *args, **kwargs):
        kwargs.pop("user", None)
        super(ProfileForm, self).__init__(*args, **kwargs)
        self.fields["profile_image"].required = False
        self.fields["background_image"].required = False

    def clean_profile_image(self):
        profile_image = self.cleaned_data.get("profile_image")
        if profile_image and isinstance(profile_image, UploadedFile):
            if profile_image.size > 5 * 1024 * 1024:
                raise ValidationError(
                    _("File size exceeds the maximum allowed limit of 5MB.")
                )
        return profile_image

    def clean_background_image(self):
        background_image = self.cleaned_data.get("background_image")
        if background_image and isinstance(background_image, UploadedFile):
            if background_image.size > 5 * 1024 * 1024:
                raise ValidationError(
                    _("File size exceeds the maximum allowed limit of 5MB.")
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
                # Save submission file using default_storage (works with S3 and local)
                storage_path = f"submissions/{self.source_file_name}"
                default_storage.save(storage_path, self.files["source_file"])
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
            "cover_image",
            "admins",
            "moderators",
            "is_open",
        ]
        widgets = {
            "admins": HeavySelect2MultipleWidget(data_view="profile_select2"),
            "moderators": HeavySelect2MultipleWidget(data_view="profile_select2"),
            "organization_image": ImageWidget,
            "cover_image": ImageWidget,
        }
        if HeavyPreviewPageDownWidget is not None:
            widgets["about"] = HeavyPreviewPageDownWidget(
                preview=reverse_lazy("organization_preview")
            )

    def __init__(self, *args, **kwargs):
        self.org_id = kwargs.pop("org_id", 0)
        super(EditOrganizationForm, self).__init__(*args, **kwargs)
        self.fields["organization_image"].required = False
        self.fields["cover_image"].required = False
        for field in ["admins", "moderators"]:
            self.fields[field].widget.data_url = (
                self.fields[field].widget.get_url() + f"?org_id={self.org_id}"
            )

    def clean_organization_image(self):
        organization_image = self.cleaned_data.get("organization_image")
        if organization_image and isinstance(organization_image, UploadedFile):
            if organization_image.size > 5 * 1024 * 1024:
                raise ValidationError(
                    _("File size exceeds the maximum allowed limit of 5MB.")
                )
        return organization_image

    def clean_cover_image(self):
        cover_image = self.cleaned_data.get("cover_image")
        if cover_image and isinstance(cover_image, UploadedFile):
            if cover_image.size > 5 * 1024 * 1024:
                raise ValidationError(
                    _("File size exceeds the maximum allowed limit of 5MB.")
                )
        return cover_image


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
        self.request = kwargs.pop("request", None)
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

        # Set user's preferred ace theme for format_config
        if self.request and hasattr(self.request, "profile"):
            self.fields["format_config"].widget.theme = self.request.profile.ace_theme

    def clean(self):
        cleaned_data = super().clean()
        format_name = cleaned_data.get("format_name")
        format_config = cleaned_data.get("format_config")

        if format_name and format_config:
            try:
                format_class = contest_format.formats[format_name]
                format_class.validate(format_config)
            except Exception as e:
                self.add_error("format_config", str(e))

        return cleaned_data

    class Meta:
        model = Contest
        fields = (
            "is_visible",
            "key",
            "name",
            "start_time",
            "end_time",
            "format_name",
            "format_config",
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
            "format_config": AceWidget(mode="json", width="100%", height="200px"),
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
        ContestProblemForm, formset=ContestProblemModelFormSet, extra=0, can_delete=True
    )
):
    model = ContestProblem


class ContestQuizForm(ModelForm):
    class Meta:
        model = ContestProblem
        fields = (
            "order",
            "quiz",
            "points",
        )
        widgets = {
            "quiz": HeavySelect2Widget(
                data_view="quiz_select2", attrs={"style": "width: 100%"}
            ),
        }


class ContestQuizModelFormSet(BaseModelFormSet):
    def is_valid(self):
        valid = super().is_valid()

        if not valid:
            return valid

        quizzes = set()
        duplicates = []

        for form in self.forms:
            if form.cleaned_data and not form.cleaned_data.get("DELETE", False):
                quiz = form.cleaned_data.get("quiz")
                if quiz in quizzes:
                    duplicates.append(quiz)
                else:
                    quizzes.add(quiz)

        if duplicates:
            for form in self.forms:
                quiz = form.cleaned_data.get("quiz")
                if quiz in duplicates:
                    form.add_error("quiz", _("This quiz is duplicated."))
            return False

        return True


class ContestQuizFormSet(
    formset_factory(
        ContestQuizForm, formset=ContestQuizModelFormSet, extra=0, can_delete=True
    )
):
    model = ContestProblem


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


class AddCourseForm(ModelForm):
    organizations = forms.ModelMultipleChoiceField(
        queryset=Organization.objects.none(),
        required=False,
        widget=HeavySelect2MultipleWidget(
            data_view="organization_select2", attrs={"style": "width: 100%"}
        ),
        label=_("Organizations"),
        help_text=_(
            "Select organizations for this course. Leave empty for public course."
        ),
    )

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        self.organization = kwargs.pop("organization", None)
        super(AddCourseForm, self).__init__(*args, **kwargs)

        # Set organizations queryset based on user permissions
        if self.request:
            admin_org_ids = self.request.profile.get_admin_organization_ids()
            if admin_org_ids:
                self.fields["organizations"].queryset = Organization.objects.filter(
                    id__in=admin_org_ids
                )
            else:
                self.fields["organizations"].queryset = Organization.objects.none()

        # Pre-select organization if provided
        if self.organization:
            self.fields["organizations"].initial = [self.organization]

    def clean(self):
        cleaned_data = super().clean()

        # Validation for non-superusers creating courses
        if self.request and not self.request.user.is_superuser:
            # For new courses, non-superusers must select at least one organization
            organizations = cleaned_data.get("organizations", [])
            if not organizations:
                raise ValidationError(
                    _(
                        "You must select at least one organization when creating a course."
                    )
                )

        return cleaned_data

    def save(self, commit=True):
        course = super().save(commit=commit)
        if commit:
            # Handle organizations assignment
            organizations = self.cleaned_data.get("organizations", [])
            if organizations:
                course.organizations.set(organizations)
                course.is_organization_private = True
                # Don't automatically set is_public to False - let user control it
            else:
                course.organizations.clear()
                course.is_organization_private = False
            course.save()
        return course

    class Meta:
        model = Course
        fields = [
            "name",
            "slug",
            "about",
            "is_public",
            "is_open",
            "course_image",
            "organizations",
        ]
        widgets = {
            "course_image": ImageWidget,
        }
        help_texts = {
            "name": _("Required. Maximum 128 characters."),
            "about": _("Optional. Detailed description of the course."),
            "slug": _(
                "Required. Course name shown in URL. Only alphanumeric characters and hyphens."
            ),
            "is_public": _("Whether this course is visible to all users"),
            "is_open": _("If checked, users can join this course"),
            "course_image": _(
                "Optional. Upload an image for the course (maximum 5MB)."
            ),
        }
        if HeavyPreviewPageDownWidget is not None:
            widgets["about"] = HeavyPreviewPageDownWidget(
                preview=reverse_lazy("blog_preview"),
                attrs={"style": "width: 100%; min-height: 300px;"},
            )


MEMORY_UNITS = (("KB", "KB"), ("MB", "MB"))


class ProblemEditForm(ModelForm):
    change_message = forms.CharField(
        max_length=256, label="Edit reason", required=False
    )
    memory_unit = forms.ChoiceField(choices=MEMORY_UNITS)

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super(ProblemEditForm, self).__init__(*args, **kwargs)
        self.fields["authors"].widget.can_add_related = False
        self.fields["curators"].widget.can_add_related = False
        self.fields["testers"].widget.can_add_related = False
        self.fields["change_message"].widget.attrs.update(
            {
                "placeholder": _("Describe the changes you made (optional)"),
            }
        )

    def clean_code(self):
        code = self.cleaned_data.get("code")

        # Check for duplicate codes, excluding the current problem's code
        existing_problem = Problem.objects.filter(code=code)

        # If editing an existing problem, exclude its own code from the check
        if self.instance.pk:
            existing_problem = existing_problem.exclude(pk=self.instance.pk)

        if existing_problem.exists():
            raise ValidationError(_("A problem with this code already exists."))

        return code

    def clean(self):
        cleaned_data = super().clean()
        memory_unit = cleaned_data.get("memory_unit", "KB")
        if memory_unit == "MB" and "memory_limit" in cleaned_data:
            cleaned_data["memory_limit"] *= 1024
        date = cleaned_data.get("date")
        if not date or date > timezone.now():
            cleaned_data["date"] = timezone.now()

        # Validate when non-admin users try to make problem public without organizations
        organizations = cleaned_data.get("organizations")
        is_public = cleaned_data.get("is_public", False)

        if self.user and not self.user.is_superuser and self.instance:
            # Get the original values from the database
            original_is_public = self.instance.is_public
            original_organizations = self.instance.organizations.all()

            # Check if new state is public with no organizations
            new_has_no_orgs = not organizations or organizations.count() == 0
            original_has_no_orgs = original_organizations.count() == 0

            if is_public and new_has_no_orgs:
                # If trying to make public without orgs, old state must be the same
                if not (original_is_public and original_has_no_orgs):
                    raise ValidationError(
                        _(
                            "You cannot publish this problem without selecting at least one organization."
                        )
                    )

        return cleaned_data

    def non_field_errors(self):
        # Check if there are any non-field errors
        errors = super().non_field_errors()

        # Collect potential non-field errors from the form
        if hasattr(self, "_non_field_errors"):
            errors.extend(self._non_field_errors)

        return errors

    class Meta:
        model = Problem
        fields = [
            # Content fields
            "code",
            "name",
            "is_public",
            "organizations",
            "date",
            "authors",
            "curators",
            "testers",
            "description",
            "pdf_description",
            # Taxonomy fields
            "types",
            "group",
            # Points fields
            "points",
            "partial",
            "short_circuit",
            # Limits fields
            "time_limit",
            "memory_limit",
            # Language fields
            "allowed_languages",
        ]
        widgets = {
            "authors": HeavySelect2MultipleWidget(
                data_view="profile_select2",
                attrs={
                    "style": "width: 50%",
                    "class": "django-select2",
                    "placeholder": _("Search and select authors"),
                },
            ),
            "curators": HeavySelect2MultipleWidget(
                data_view="profile_select2",
                attrs={
                    "style": "width: 50%",
                    "class": "django-select2",
                    "placeholder": _("Search and select curators"),
                },
            ),
            "testers": HeavySelect2MultipleWidget(
                data_view="profile_select2",
                attrs={
                    "style": "width: 50%",
                    "class": "django-select2",
                    "placeholder": _("Search and select testers"),
                },
            ),
            "organizations": HeavySelect2MultipleWidget(
                data_view="organization_select2",
                attrs={
                    "style": "width: 50%",
                    "class": "django-select2",
                    "placeholder": _("Search and select organizations"),
                },
            ),
            "types": Select2MultipleWidget(
                attrs={
                    "style": "width: 50%",
                    "class": "django-select2",
                    "placeholder": _("Search and select problem types"),
                }
            ),
            "group": Select2Widget(
                attrs={
                    "style": "width: 30%",
                    "class": "django-select2",
                    "placeholder": _("Search and select problem group"),
                }
            ),
            "memory_limit": forms.TextInput(attrs={"size": "20"}),
            "time_limit": forms.NumberInput(attrs={"step": "0.1"}),
            "points": forms.NumberInput(attrs={"step": "0.5"}),
            "allowed_languages": forms.CheckboxSelectMultiple(),
            "date": DateTimePickerWidget(),
            "pdf_description": PDFWidget(),
        }

        if HeavyPreviewPageDownWidget is not None:
            widgets["description"] = HeavyPreviewPageDownWidget(
                preview=reverse_lazy("problem_preview")
            )


class ProblemAddForm(ModelForm):
    memory_unit = forms.ChoiceField(choices=MEMORY_UNITS, initial="KB")

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super(ProblemAddForm, self).__init__(*args, **kwargs)
        self.fields["authors"].widget.can_add_related = False
        self.fields["curators"].widget.can_add_related = False
        self.fields["testers"].widget.can_add_related = False

        # Set current user as default author
        if self.user and self.user.is_authenticated:
            self.fields["authors"].initial = [self.user.profile]

    def clean_code(self):
        code = self.cleaned_data.get("code")
        if Problem.objects.filter(code=code).exists():
            raise ValidationError(_("A problem with this code already exists."))
        return code

    def clean(self):
        cleaned_data = super().clean()
        memory_unit = cleaned_data.get("memory_unit", "KB")
        if memory_unit == "MB" and "memory_limit" in cleaned_data:
            cleaned_data["memory_limit"] *= 1024
        date = cleaned_data.get("date")
        if not date or date > timezone.now():
            cleaned_data["date"] = timezone.now()

        # Validate when non-admin users try to create public problem without organizations
        organizations = cleaned_data.get("organizations")
        is_public = cleaned_data.get("is_public", False)

        if self.user and not self.user.is_superuser:
            # For new problems, prevent creating public problems without organizations
            new_has_no_orgs = not organizations or organizations.count() == 0

            if is_public and new_has_no_orgs:
                raise ValidationError(
                    _(
                        "You cannot create a public problem without selecting at least one organization."
                    )
                )

        return cleaned_data

    class Meta:
        model = Problem
        fields = [
            # Content fields
            "code",
            "name",
            "is_public",
            "organizations",
            "date",
            "authors",
            "curators",
            "testers",
            "description",
            "pdf_description",
            # Taxonomy fields
            "types",
            "group",
            # Points fields
            "points",
            "partial",
            "short_circuit",
            # Limits fields
            "time_limit",
            "memory_limit",
            # Language fields
            "allowed_languages",
        ]
        widgets = {
            "authors": HeavySelect2MultipleWidget(
                data_view="profile_select2",
                attrs={
                    "style": "width: 50%",
                    "class": "django-select2",
                    "placeholder": _("Search and select authors"),
                },
            ),
            "curators": HeavySelect2MultipleWidget(
                data_view="profile_select2",
                attrs={
                    "style": "width: 50%",
                    "class": "django-select2",
                    "placeholder": _("Search and select curators"),
                },
            ),
            "testers": HeavySelect2MultipleWidget(
                data_view="profile_select2",
                attrs={
                    "style": "width: 50%",
                    "class": "django-select2",
                    "placeholder": _("Search and select testers"),
                },
            ),
            "organizations": HeavySelect2MultipleWidget(
                data_view="organization_select2",
                attrs={
                    "style": "width: 50%",
                    "class": "django-select2",
                    "placeholder": _("Search and select organizations"),
                },
            ),
            "types": Select2MultipleWidget(
                attrs={
                    "style": "width: 50%",
                    "class": "django-select2",
                    "placeholder": _("Search and select problem types"),
                }
            ),
            "group": Select2Widget(
                attrs={
                    "style": "width: 30%",
                    "class": "django-select2",
                    "placeholder": _("Search and select problem group"),
                }
            ),
            "memory_limit": forms.TextInput(attrs={"size": "20"}),
            "time_limit": forms.NumberInput(attrs={"step": "0.1"}),
            "points": forms.NumberInput(attrs={"step": "0.5"}),
            "allowed_languages": forms.CheckboxSelectMultiple(),
            "date": DateTimePickerWidget(),
        }

        if HeavyPreviewPageDownWidget is not None:
            widgets["description"] = HeavyPreviewPageDownWidget(
                preview=reverse_lazy("problem_preview")
            )


class LanguageLimitEditForm(ModelForm):
    memory_unit = forms.ChoiceField(
        choices=MEMORY_UNITS, label=_("Memory unit"), initial="KB"
    )

    def __init__(self, *args, **kwargs):
        problem = kwargs.pop("problem", None)
        super().__init__(*args, **kwargs)
        self.problem = problem
        if problem:
            # Limit language choices to problem's allowed languages
            self.fields["language"].queryset = problem.allowed_languages.order_by(
                "name"
            )

        # Make all fields required
        self.fields["language"].required = True
        self.fields["time_limit"].required = True
        self.fields["memory_limit"].required = True

        # Add form styling
        self.fields["language"].widget.attrs.update({"class": "form-control"})
        self.fields["time_limit"].widget.attrs.update(
            {"class": "form-control", "step": "0.1"}
        )
        self.fields["memory_limit"].widget.attrs.update(
            {"class": "form-control", "min": "1"}
        )
        self.fields["memory_unit"].widget.attrs.update({"class": "form-select"})

    def clean(self):
        cleaned_data = super().clean()

        # Check for duplicate language limit
        language = cleaned_data.get("language")
        if language and self.problem:
            existing_limit = LanguageLimit.objects.filter(
                problem=self.problem, language=language
            )
            if existing_limit.exists():
                raise ValidationError(
                    {
                        "language": _(
                            "A language limit for this language already exists for this problem."
                        )
                    }
                )

        # Validate that time and memory limits are positive
        time_limit = cleaned_data.get("time_limit")
        memory_limit = cleaned_data.get("memory_limit")

        if time_limit is not None and time_limit <= 0:
            raise ValidationError({"time_limit": _("Time limit must be positive.")})

        if memory_limit is not None and memory_limit <= 0:
            raise ValidationError({"memory_limit": _("Memory limit must be positive.")})

        # Convert memory limit based on selected unit
        memory_unit = cleaned_data.get("memory_unit", "KB")

        # Convert memory limit if it's in MB to KB
        if memory_limit is not None and memory_unit == "MB":
            cleaned_data["memory_limit"] = int(memory_limit * 1024)

        # Remove memory_unit from cleaned_data since it's not a model field
        cleaned_data.pop("memory_unit", None)

        return cleaned_data

    class Meta:
        model = LanguageLimit
        fields = ["language", "time_limit", "memory_limit"]
        widgets = {
            "language": Select2Widget(
                attrs={
                    "style": "width: 50%",
                    "class": "django-select2",
                    "placeholder": "Search and select language",
                }
            ),
            "memory_limit": forms.TextInput(attrs={"size": "20"}),
            "time_limit": forms.TextInput(attrs={"step": "0.3"}),
        }


class LanguageTemplateEditForm(ModelForm):
    def __init__(self, *args, **kwargs):
        problem = kwargs.pop("problem", None)
        super().__init__(*args, **kwargs)
        self.problem = problem
        if problem:
            # Limit language choices to problem's allowed languages
            self.fields["language"].queryset = problem.allowed_languages.order_by(
                "name"
            )

        # Make fields required
        self.fields["language"].required = True
        self.fields["source"].required = True

        # Add form styling
        self.fields["language"].widget.attrs.update({"class": "form-control"})

    def clean(self):
        cleaned_data = super().clean()

        # Check for duplicate language template
        language = cleaned_data.get("language")
        if language and self.problem:
            existing_template = LanguageTemplate.objects.filter(
                problem=self.problem, language=language
            )
            if existing_template.exists():
                raise ValidationError(
                    {
                        "language": _(
                            "A language template for this language already exists for this problem."
                        )
                    }
                )

        return cleaned_data

    class Meta:
        model = LanguageTemplate
        fields = ["language", "source"]
        widgets = {
            "source": AceWidget(width="100%", height="300px", toolbar=False),
        }


class ProblemSolutionEditForm(ModelForm):
    def __init__(self, *args, **kwargs):
        super(ProblemSolutionEditForm, self).__init__(*args, **kwargs)
        self.fields["authors"].widget.can_add_related = False

        # Set default values for new solutions
        if not self.instance.pk:
            # Set default publish date to now if not specified
            if not self.initial.get("publish_on"):
                self.initial["publish_on"] = timezone.now()
            # Set default to private if not specified
            if not self.initial.get("is_public"):
                self.initial["is_public"] = False

        # Add helpful help text
        self.fields["is_public"].help_text = _(
            "Must be checked for the editorial to be visible to users."
        )
        self.fields["publish_on"].help_text = _(
            "Editorial will only be visible after this date/time."
        )
        self.fields["content"].help_text = _(
            "The editorial content explaining the solution approach."
        )
        self.fields["authors"].help_text = _(
            "Authors who contributed to this editorial solution."
        )

    class Meta:
        model = Solution
        fields = ["is_public", "publish_on", "authors", "content"]
        widgets = {
            "authors": HeavySelect2MultipleWidget(
                data_view="profile_select2", attrs={"style": "width: 50%"}
            ),
            "publish_on": DateTimePickerWidget(),
        }

        if HeavyPreviewPageDownWidget is not None:
            widgets["content"] = HeavyPreviewPageDownWidget(
                preview=reverse_lazy("solution_preview")
            )


class ProblemTranslationEditForm(ModelForm):
    class Meta:
        model = ProblemTranslation
        fields = ["language", "name", "description"]
        if HeavyPreviewPageDownWidget is not None:
            widgets = {
                "description": HeavyPreviewPageDownWidget(
                    preview=reverse_lazy("problem_preview")
                )
            }
