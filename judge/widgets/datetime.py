from django import forms
from django.utils.html import format_html
from django.forms.utils import flatatt
from django.utils.dateparse import parse_datetime, parse_date


class DateTimePickerWidget(forms.DateTimeInput):
    input_type = "datetime-local"

    def render(self, name, value, attrs=None, renderer=None):
        if value is None:
            value = ""
        elif isinstance(value, str):
            # Attempt to parse the string back to datetime
            parsed_date = parse_datetime(value)
            if parsed_date is not None:
                value = parsed_date.strftime("%Y-%m-%dT%H:%M")
            else:
                value = ""
        else:
            value = value.strftime("%Y-%m-%dT%H:%M")

        final_attrs = self.build_attrs(
            attrs, {"type": self.input_type, "name": name, "value": value}
        )
        return format_html("<input{}>", flatatt(final_attrs))


class DatePickerWidget(forms.DateInput):
    input_type = "date"

    def render(self, name, value, attrs=None, renderer=None):
        if value is None:
            value = ""
        elif isinstance(value, str):
            # Attempt to parse the string back to date
            parsed_date = parse_date(value)
            if parsed_date is not None:
                value = parsed_date.strftime("%Y-%m-%d")
            else:
                value = ""
        else:
            value = value.strftime("%Y-%m-%d")

        final_attrs = self.build_attrs(
            attrs, {"type": self.input_type, "name": name, "value": value}
        )
        return format_html("<input{}>", flatatt(final_attrs))
