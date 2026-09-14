from django.contrib import admin
from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms import ModelForm, modelform_factory
from django.http import HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.utils.html import format_html
from django.utils.translation import gettext, gettext_lazy as _
from reversion.admin import VersionAdmin

from judge.models import Organization, OfficialSchool, Profile
from judge.services.official_school import (
    can_manage_school,
    configure_school,
    create_school,
)
from judge.widgets import (
    AdminHeavySelect2MultipleWidget,
    AdminHeavySelect2Widget,
    HeavyPreviewAdminPageDownWidget,
)


class OrganizationForm(ModelForm):
    def _save_m2m(self):
        if self.instance.has_school() and "admins" in self.cleaned_data:
            self.instance.admins.add(*self.cleaned_data["admins"])
        super()._save_m2m()

    def clean_admins(self):
        admins = self.cleaned_data["admins"]
        if self.instance.pk and self.instance.has_school() and not admins:
            raise ValidationError(
                _("An official school needs at least one administrator.")
            )
        return admins

    class Meta:
        widgets = {
            "admins": AdminHeavySelect2MultipleWidget(data_view="profile_select2"),
            "moderators": AdminHeavySelect2MultipleWidget(data_view="profile_select2"),
            "registrant": AdminHeavySelect2Widget(data_view="profile_select2"),
        }
        if HeavyPreviewAdminPageDownWidget is not None:
            widgets["about"] = HeavyPreviewAdminPageDownWidget(
                preview=reverse_lazy("organization_preview")
            )


class OrganizationAdmin(VersionAdmin):
    readonly_fields = ("creation_date",)
    fields = (
        "name",
        "slug",
        "short_name",
        "is_open",
        "is_community",
        "about",
        "slots",
        "registrant",
        "creation_date",
        "admins",
        "moderators",
    )
    list_display = (
        "name",
        "short_name",
        "is_open",
        "is_community",
        "creation_date",
        "registrant",
        "show_public",
    )
    list_filter = ("is_open", "is_community")
    search_fields = ("name", "short_name", "registrant__user__username")
    prepopulated_fields = {"slug": ("name",)}
    actions_on_top = True
    actions_on_bottom = True
    form = OrganizationForm
    ordering = ["-creation_date"]

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if object_id and str(object_id).isdigit():
            school = (
                OfficialSchool.objects.select_related("organization")
                .filter(pk=object_id)
                .first()
            )
            if school:
                if request.user.is_superuser:
                    target = reverse(
                        "admin:judge_officialschool_change", args=[school.pk]
                    )
                elif self.has_change_permission(request, school.organization):
                    target = reverse(
                        "edit_organization", args=[school.pk, school.organization.slug]
                    )
                else:
                    raise PermissionDenied
                if request.method == "POST":
                    messages.info(
                        request,
                        _(
                            "School settings have moved. Please make your changes on this page."
                        ),
                    )
                return HttpResponseRedirect(target)
        return super().changeform_view(request, object_id, form_url, extra_context)

    def show_public(self, obj):
        return format_html(
            '<a href="{0}" style="white-space:nowrap;">{1}</a>',
            obj.get_absolute_url(),
            gettext("View on site"),
        )

    show_public.short_description = ""

    def get_readonly_fields(self, request, obj=None):
        fields = self.readonly_fields
        if obj and obj.has_school() and not request.user.is_superuser:
            return fields + ("name", "slug", "short_name", "registrant", "slots")
        if not request.user.has_perm("judge.organization_admin"):
            return fields + ("registrant", "admins", "is_open", "slots")
        return fields

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if obj and obj.has_school():
            return [
                field for field in fields if field not in ("is_open", "is_community")
            ]
        return fields

    def get_prepopulated_fields(self, request, obj=None):
        if obj and obj.has_school() and not request.user.is_superuser:
            return {}
        return super().get_prepopulated_fields(request, obj)

    def get_queryset(self, request):
        queryset = Organization.objects.filter(official_school__isnull=True)
        if request.user.has_perm("judge.edit_all_organization"):
            return queryset
        else:
            return queryset.filter(admins=request.profile.id)

    def has_change_permission(self, request, obj=None):
        if obj and obj.has_school():
            return request.user.is_superuser or (
                request.user.has_perm("judge.change_organization")
                and obj.admins.filter(pk=request.profile.pk).exists()
            )
        if not request.user.has_perm("judge.change_organization"):
            return False
        if request.user.has_perm("judge.edit_all_organization") or obj is None:
            return True
        return obj.admins.filter(id=request.profile.id).exists()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        if not obj.has_school():
            obj.members.add(*obj.admins.all())

    def save_model(self, request, obj, form, change):
        if obj.pk and obj.has_school():
            locked = Organization.objects.select_for_update().get(pk=obj.pk)
            if not can_manage_school(request.user, obj.pk):
                raise PermissionDenied
            if not request.user.is_superuser:
                for field in ("name", "slug", "short_name", "registrant_id", "slots"):
                    setattr(obj, field, getattr(locked, field))
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.has_school():
            return False
        return super().has_delete_permission(request, obj)


SchoolOrganizationForm = modelform_factory(
    Organization,
    form=OrganizationForm,
    fields=("name", "slug", "short_name", "about", "slots", "admins"),
)


class OfficialSchoolForm(ModelForm):
    name = SchoolOrganizationForm.base_fields["name"]
    slug = SchoolOrganizationForm.base_fields["slug"]
    short_name = SchoolOrganizationForm.base_fields["short_name"]
    about = SchoolOrganizationForm.base_fields["about"]
    slots = SchoolOrganizationForm.base_fields["slots"]
    admins = SchoolOrganizationForm.base_fields["admins"]
    new_name = forms.CharField(
        label=_("New school name"), max_length=128, required=False
    )
    new_slug = forms.SlugField(
        label=_("New school slug"), max_length=128, required=False
    )
    new_short_name = forms.CharField(
        label=_("New school short name"), max_length=20, required=False
    )
    teachers = forms.ModelMultipleChoiceField(
        label=_("Initial school administrators"),
        queryset=Profile.objects.all(),
        required=False,
        widget=AdminHeavySelect2MultipleWidget(
            data_view="profile_select2", attrs={"data-width": "100%"}
        ),
    )

    class Meta:
        model = OfficialSchool
        fields = ("is_active",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in ("new_name", "new_slug", "new_short_name", "teachers"):
            if field in self.fields:
                self.fields[field].required = not self.instance.pk
        if self.instance.pk:
            self.organization_form = SchoolOrganizationForm(
                data=self.data if self.is_bound else None,
                files=self.files if self.is_bound else None,
                instance=self.instance.organization,
                prefix=self.prefix,
            )
            self.organization_form.fields["about"].required = False
            for name, field in self.organization_form.fields.items():
                if name in self.fields:
                    self.fields[name] = field
                    self.initial[name] = self.organization_form.initial.get(name)
            for name in ("admins",):
                if name in self.fields:
                    self.fields[name].widget.attrs["data-width"] = "100%"
        else:
            for name in SchoolOrganizationForm.base_fields:
                self.fields.pop(name, None)

    def clean(self):
        data = super().clean()
        if self.instance.pk:
            if not self.organization_form.is_valid():
                for name, errors in self.organization_form.errors.items():
                    self.add_error(name if name in self.fields else None, errors)
        else:
            if (
                data.get("new_slug")
                and Organization.objects.filter(slug=data["new_slug"]).exists()
            ):
                self.add_error(
                    "new_slug", _("An organization with this slug already exists.")
                )
        return data


class OfficialSchoolAdmin(admin.ModelAdmin):
    form = OfficialSchoolForm
    list_display = ("organization", "is_active", "verified_at", "verified_by")
    list_select_related = ("organization", "verified_by__user")
    search_fields = ("organization__name", "organization__slug")

    class Media:
        css = {"all": ("admin/official-school.css",)}

    def view_on_site(self, obj):
        return obj.organization.get_absolute_url()

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except ValidationError as error:
            messages.error(request, "; ".join(error.messages))
            return HttpResponseRedirect(request.get_full_path())

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return ("verified_at", "verified_by") if obj else ()

    def get_fields(self, request, obj=None):
        if obj:
            return (
                "name",
                "slug",
                "short_name",
                "about",
                "admins",
                "slots",
                "is_active",
                "verified_at",
                "verified_by",
            )
        return (
            "new_name",
            "new_slug",
            "new_short_name",
            "teachers",
        )

    def save_model(self, request, obj, form, change):
        if change:
            org = Organization.objects.select_for_update().get(pk=obj.pk)
            data = form.organization_form.cleaned_data
            fields = ("name", "slug", "short_name", "about", "slots")
            for name in fields:
                setattr(org, name, data[name])
            org.save(update_fields=fields)
            # Add successors before removing former administrators.
            org.admins.add(*data["admins"])
            org.admins.set(data["admins"])
            org.moderators.clear()
            school = configure_school(
                request.user,
                org,
                is_active=obj.is_active,
            )
        else:
            school = create_school(
                request.user,
                name=form.cleaned_data["new_name"],
                slug=form.cleaned_data["new_slug"],
                short_name=form.cleaned_data["new_short_name"],
                teachers=form.cleaned_data["teachers"],
            )
        obj.organization = school.organization
        obj.pk = school.pk
        obj.verified_by = school.verified_by
        obj.verified_at = school.verified_at
        obj._state.adding = False


class OrganizationRequestAdmin(admin.ModelAdmin):
    list_display = ("username", "organization", "state", "time")
    readonly_fields = ("user", "organization")

    def username(self, obj):
        return obj.user.user.username

    username.short_description = _("username")
    username.admin_order_field = "user__user__username"
