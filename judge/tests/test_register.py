from unittest.mock import MagicMock, patch

from django.db import IntegrityError
from django.test import SimpleTestCase, override_settings

from judge.views.register import OldRegistrationView, RegistrationView


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
