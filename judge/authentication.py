from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import User


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
