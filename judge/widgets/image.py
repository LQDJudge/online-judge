from django import forms


class ImageWidget(forms.ClearableFileInput):
    template_name = "widgets/image.html"

    def __init__(self, attrs=None, width=80, height=80):
        self.width = width
        self.height = height
        super().__init__(attrs)

    def get_context(self, name, value, attrs=None):
        context = super().get_context(name, value, attrs)
        context["widget"]["height"] = self.height
        context["widget"]["width"] = self.height
        return context
