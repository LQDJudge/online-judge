import unicodedata

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

USERNAME_ALLOWED_MESSAGE = _(
    "Username can only contain Latin letters, digits, and underscores."
)


def normalize_username(username):
    return unicodedata.normalize("NFC", username or "")


def clean_username(username):
    username = normalize_username(username)
    validate_username(username)
    return username


def validate_username(username):
    if not username:
        raise ValidationError(USERNAME_ALLOWED_MESSAGE)

    for char in username:
        if is_allowed_username_char(char):
            continue
        raise ValidationError(USERNAME_ALLOWED_MESSAGE)


def is_allowed_username_char(char):
    return char == "_" or "0" <= char <= "9" or _is_latin_letter(char)


def _is_latin_letter(char):
    if char in {"Đ", "đ"}:
        return True
    if not unicodedata.category(char).startswith("L"):
        return False

    decomposed = unicodedata.normalize("NFKD", char)
    letters = [
        decomposed_char
        for decomposed_char in decomposed
        if unicodedata.category(decomposed_char).startswith("L")
    ]
    if len(letters) != 1 or not _is_ascii_letter(letters[0]):
        return False

    return all(
        decomposed_char == letters[0]
        or unicodedata.category(decomposed_char).startswith("M")
        for decomposed_char in decomposed
    )


def _is_ascii_letter(char):
    return "A" <= char <= "Z" or "a" <= char <= "z"
