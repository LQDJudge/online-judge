from django import forms
from django.templatetags.static import static
from django.utils.html import format_html
from django.forms.utils import flatatt


class DateTimePickerWidget(forms.DateTimeInput):
    input_type = "datetime-local"

    def render(self, name, value, attrs=None, renderer=None):
        if value is None:
            value = ""
        else:
            value = value.strftime("%Y-%m-%dT%H:%M")

        final_attrs = self.build_attrs(
            attrs, {"type": self.input_type, "name": name, "value": value}
        )
        return format_html("<input{}>", flatatt(final_attrs))
