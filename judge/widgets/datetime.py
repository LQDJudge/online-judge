from django import forms


class DateTimePickerWidget(forms.DateTimeInput):
    template_name = "widgets/datetimepicker.html"

    def get_context(self, name, value, attrs):
        datetimepicker_id = "datetimepicker_{name}".format(name=name)
        if attrs is None:
            attrs = dict()
        attrs["data-target"] = "#{id}".format(id=datetimepicker_id)
        attrs["class"] = "form-control datetimepicker-input"
        context = super().get_context(name, value, attrs)
        context["widget"]["datetimepicker_id"] = datetimepicker_id
        return context

    @property
    def media(self):
        css_url = "/static/datetime-picker/datetimepicker.min.css"
        js_url = "/static/datetime-picker/datetimepicker.full.min.js"
        return forms.Media(
            js=[js_url],
            css={"screen": [css_url]},
        )
