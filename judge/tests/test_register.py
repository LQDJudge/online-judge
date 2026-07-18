from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase, override_settings

from judge.admin.profile import UserForm
from judge.social_auth import UsernameForm, slugify_username
from judge.validators import clean_username
from judge.views.register import OldRegistrationView, RegistrationView
from judge.views.register import CustomRegistrationForm


class UsernameValidatorTest(SimpleTestCase):
    def test_allows_latin_vietnamese_usernames(self):
        self.assertEqual(clean_username("Thỏ"), "Thỏ")
        self.assertEqual(clean_username("Phụng_Nguyễn"), "Phụng_Nguyễn")
        self.assertEqual(clean_username("Đặng_Thị_Thỏ"), "Đặng_Thị_Thỏ")
        self.assertEqual(clean_username("Nguyen_123"), "Nguyen_123")

    def test_normalizes_decomposed_vietnamese_usernames(self):
        self.assertEqual(clean_username("Tho\u0309"), "Thỏ")

    def test_rejects_non_latin_or_invisible_usernames(self):
        bad_usernames = [
            "аdmіn",
            "аԁmіո",
            "uоu",
            "Prototypeᅠ",
            "ᅠᅠᅠ",
            "admın",
            "Godɢ",
            "user-name",
            "user name",
        ]
        for username in bad_usernames:
            with self.subTest(username=username):
                with self.assertRaises(ValidationError):
                    clean_username(username)


@override_settings(LANGUAGE_CODE="en")
class UsernameFormValidationTest(TestCase):
    def test_registration_form_rejects_homograph_username(self):
        form = CustomRegistrationForm()
        form.cleaned_data = {"username": "аdmіn"}

        with self.assertRaises(ValidationError):
            form.clean_username()

    def test_registration_form_normalizes_before_duplicate_check(self):
        User.objects.create_user(username="Thỏ")
        form = CustomRegistrationForm()
        form.cleaned_data = {"username": "Tho\u0309"}

        with self.assertRaises(ValidationError) as context:
            form.clean_username()

        self.assertEqual(
            str(context.exception), "['A user with that username already exists.']"
        )

    def test_social_username_form_uses_same_validator(self):
        form = UsernameForm(data={"username": "Nguyễn_Văn_A"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["username"], "Nguyễn_Văn_A")

        form = UsernameForm(data={"username": "uоu"})
        self.assertFalse(form.is_valid())
        self.assertIn("username", form.errors)

    def test_social_username_suggestion_uses_same_character_policy(self):
        self.assertEqual(slugify_username("Tho\u0309-Nguyen"), "Thỏ_Nguyen")
        self.assertEqual(slugify_username("аdmіn"), "dmn")

    def test_admin_user_form_uses_same_validator(self):
        form = UserForm()
        form.cleaned_data = {"username": "Phụng_Nguyễn"}
        self.assertEqual(form.clean_username(), "Phụng_Nguyễn")

        form.cleaned_data = {"username": "Prototypeᅠ"}
        with self.assertRaises(ValidationError):
            form.clean_username()

    def test_admin_user_form_allows_unchanged_legacy_username(self):
        user = User.objects.create_user(username="аdmіn")
        form = UserForm(instance=user)
        form.cleaned_data = {"username": "аdmіn"}
        self.assertEqual(form.clean_username(), "аdmіn")

        form.cleaned_data = {"username": "uоu"}
        with self.assertRaises(ValidationError):
            form.clean_username()


@override_settings(LANGUAGE_CODE="en")
class RegistrationViewIntegrityErrorTest(SimpleTestCase):
    def test_duplicate_username_integrity_error_returns_form_error(self):
        view = RegistrationView()
        form = MagicMock()
        expected_response = object()
        error = IntegrityError(
            1062, "Duplicate entry 'iwin68clubitcom' for key 'username'"
        )

        with patch.object(OldRegistrationView, "form_valid", side_effect=error):
            with patch.object(
                RegistrationView, "form_invalid", return_value=expected_response
            ) as form_invalid:
                response = view.form_valid(form)

        self.assertIs(response, expected_response)
        form_invalid.assert_called_once_with(form)
        form.add_error.assert_called_once()
        field, message = form.add_error.call_args.args
        self.assertEqual(field, "username")
        self.assertEqual(str(message), "A user with that username already exists.")

    def test_unrelated_integrity_error_is_not_suppressed(self):
        view = RegistrationView()
        form = MagicMock()
        error = IntegrityError(1062, "Duplicate entry 'x' for key 'other_key'")

        with patch.object(OldRegistrationView, "form_valid", side_effect=error):
            with self.assertRaises(IntegrityError):
                view.form_valid(form)

        form.add_error.assert_not_called()
