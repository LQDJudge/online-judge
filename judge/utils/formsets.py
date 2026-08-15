def active_formset_forms(formset, *, empty_flag="_empty_row"):
    for form in formset.forms:
        cleaned_data = getattr(form, "cleaned_data", None)
        if not cleaned_data:
            continue
        if cleaned_data.get("DELETE") or cleaned_data.get(empty_flag):
            continue
        yield form


def validate_max_active_forms(formset, max_count, message, *, empty_flag="_empty_row"):
    active_forms = list(active_formset_forms(formset, empty_flag=empty_flag))
    if len(active_forms) <= max_count:
        return True
    active_forms[max_count].add_error(None, message)
    return False
