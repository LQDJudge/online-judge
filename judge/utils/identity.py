from contextlib import contextmanager
from dataclasses import dataclass

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.forms.models import BaseInlineFormSet, BaseModelFormSet
from django.utils.translation import gettext_lazy as _

IMMUTABLE_IDENTITY_ERROR = _(
    "%(field)s cannot be changed after this row is created. Delete the row and add a new one instead."
)


class ImmutableIdentityMixin:
    immutable_identity_fields = ()
    _allow_identity_update = False

    def validate_immutable_identity(self):
        if not self.pk or self._allow_identity_update:
            return
        if not self.immutable_identity_fields:
            return

        old_values = (
            type(self)
            ._default_manager.filter(pk=self.pk)
            .values(*self.immutable_identity_fields)
            .first()
        )
        if old_values is None:
            return

        errors = []
        for field in self.immutable_identity_fields:
            if getattr(self, field) != old_values[field]:
                errors.append(
                    IMMUTABLE_IDENTITY_ERROR
                    % {"field": self._identity_field_label(field)}
                )
        if errors:
            raise ValidationError(errors)

    def _identity_field_label(self, field):
        if field.endswith("_id"):
            field = field[:-3]
        try:
            return self._meta.get_field(field).verbose_name
        except FieldDoesNotExist:
            return field.replace("_", " ")


@contextmanager
def allow_identity_update(instance):
    previous = getattr(instance, "_allow_identity_update", False)
    instance._allow_identity_update = True
    try:
        yield instance
    finally:
        instance._allow_identity_update = previous


@dataclass
class SemanticFormsetPlan:
    formset: object
    parent_field: str
    parent: object
    identity_fields: tuple
    desired: list
    deleted_objects: list
    existing_by_key: dict


def build_semantic_formset_plan(
    formset,
    *,
    parent_field,
    parent,
    identity_fields,
    empty_flag="_empty_row",
):
    model = formset.model
    initial_pks = [
        form.instance.pk
        for form in formset.initial_forms
        if getattr(form.instance, "pk", None)
    ]
    existing_objects = model._default_manager.filter(
        pk__in=initial_pks, **{parent_field: parent}
    )
    existing_by_key = {
        _object_identity_key(obj, identity_fields): obj for obj in existing_objects
    }

    desired = []
    desired_keys = set()
    duplicate_keys = set()
    for form in formset.forms:
        if not getattr(form, "cleaned_data", None):
            continue
        if form.cleaned_data.get("DELETE") or form.cleaned_data.get(empty_flag):
            continue
        key = _form_identity_key(form, identity_fields)
        if key in desired_keys:
            duplicate_keys.add(key)
        desired_keys.add(key)
        desired.append((key, form))

    if duplicate_keys:
        raise ValidationError(_("Duplicate rows are not allowed."))

    deleted_objects = [
        obj for key, obj in existing_by_key.items() if key not in desired_keys
    ]
    return SemanticFormsetPlan(
        formset=formset,
        parent_field=parent_field,
        parent=parent,
        identity_fields=tuple(identity_fields),
        desired=desired,
        deleted_objects=deleted_objects,
        existing_by_key=existing_by_key,
    )


def count_semantic_formset_deletions(
    formset,
    *,
    parent_field,
    parent,
    identity_fields,
    count_queryset,
    empty_flag="_empty_row",
):
    plan = build_semantic_formset_plan(
        formset,
        parent_field=parent_field,
        parent=parent,
        identity_fields=identity_fields,
        empty_flag=empty_flag,
    )
    if not plan.deleted_objects:
        return 0
    return count_queryset(plan.deleted_objects).count()


def save_semantic_formset(
    formset,
    *,
    parent_field,
    parent,
    identity_fields,
    empty_flag="_empty_row",
):
    plan = build_semantic_formset_plan(
        formset,
        parent_field=parent_field,
        parent=parent,
        identity_fields=identity_fields,
        empty_flag=empty_flag,
    )
    model = formset.model
    saved_objects = []
    formset.new_objects = []
    formset.changed_objects = []

    for obj in plan.deleted_objects:
        obj.delete()

    for key, form in plan.desired:
        obj = plan.existing_by_key.get(key)
        if obj is None:
            obj = model()
            setattr(obj, plan.parent_field, plan.parent)
            is_new = True
        else:
            is_new = False
        _copy_form_values(obj, form, plan.parent_field)
        setattr(obj, plan.parent_field, plan.parent)
        obj.save()
        saved_objects.append(obj)
        if is_new:
            formset.new_objects.append(obj)
        elif form.changed_data:
            formset.changed_objects.append((obj, form.changed_data))

    formset.deleted_objects = plan.deleted_objects
    return saved_objects


class SemanticIdentityValidationMixin:
    semantic_identity_fields = ()
    empty_flag = "_empty_row"

    def is_valid(self):
        valid = super().is_valid()
        if not valid:
            return valid
        if not self.semantic_identity_fields:
            return valid

        seen = {}
        duplicate_forms = set()
        for form in self.forms:
            if not getattr(form, "cleaned_data", None):
                continue
            if form.cleaned_data.get("DELETE") or form.cleaned_data.get(
                self.empty_flag
            ):
                continue
            key = _form_identity_key(form, self.semantic_identity_fields)
            if key in seen:
                duplicate_forms.add(seen[key])
                duplicate_forms.add(form)
            else:
                seen[key] = form

        if not duplicate_forms:
            return valid

        for form in duplicate_forms:
            form.add_error(None, _("Duplicate rows are not allowed."))
        return False


class SemanticIdentityModelFormSet(SemanticIdentityValidationMixin, BaseModelFormSet):
    pass


class SemanticIdentityInlineFormSet(SemanticIdentityValidationMixin, BaseInlineFormSet):
    semantic_identity_fields = ()

    def save(self, commit=True):
        if not commit:
            return super().save(commit=commit)
        return save_semantic_formset(
            self,
            parent_field=self.fk.name,
            parent=self.instance,
            identity_fields=self.semantic_identity_fields,
        )


def _copy_form_values(obj, form, parent_field):
    fields = form._meta.fields
    if fields is None:
        fields = form.cleaned_data.keys()
    for field in fields:
        if field == parent_field or field in {"id", "DELETE"}:
            continue
        if field not in form.cleaned_data:
            continue
        setattr(obj, field, form.cleaned_data[field])


def _object_identity_key(obj, identity_fields):
    return tuple(_object_identity_value(obj, field) for field in identity_fields)


def _object_identity_value(obj, field):
    model_field = obj._meta.get_field(field)
    return getattr(obj, model_field.attname)


def _form_identity_key(form, identity_fields):
    return tuple(_form_identity_value(form, field) for field in identity_fields)


def _form_identity_value(form, field):
    value = form.cleaned_data.get(field)
    return value.pk if hasattr(value, "pk") else value
