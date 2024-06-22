from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import User
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.views import PasswordChangeView
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _


class CustomModelBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        try:
            # Check if the username is an email
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            # If the username is not an email, try authenticating with the username field
            user = User.objects.filter(email=username).first()

        if user and user.check_password(password):
            return user


class CustomPasswordChangeForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super(CustomPasswordChangeForm, self).__init__(*args, **kwargs)
        if not self.user.has_usable_password():
            self.fields.pop("old_password")

    def clean_old_password(self):
        if "old_password" not in self.cleaned_data:
            return
        return super(CustomPasswordChangeForm, self).clean_old_password()

    def clean(self):
        cleaned_data = super(CustomPasswordChangeForm, self).clean()
        if "old_password" not in self.cleaned_data and not self.errors:
            cleaned_data["old_password"] = ""
        return cleaned_data


class CustomPasswordChangeView(PasswordChangeView):
    form_class = CustomPasswordChangeForm
    success_url = reverse_lazy("password_change_done")
    template_name = "registration/password_change_form.html"

    def get_form_kwargs(self):
        kwargs = super(CustomPasswordChangeView, self).get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs
