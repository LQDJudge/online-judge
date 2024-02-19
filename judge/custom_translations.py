from django.utils.translation import gettext_lazy as _, ngettext


def custom_trans():
    return [
        # Password reset
        ngettext(
            "This password is too short. It must contain at least %(min_length)d character.",
            "This password is too short. It must contain at least %(min_length)d characters.",
            0,
        ),
        ngettext(
            "Your password must contain at least %(min_length)d character.",
            "Your password must contain at least %(min_length)d characters.",
            0,
        ),
        _("The two password fields didn’t match."),
        _("Your password can’t be entirely numeric."),
        # Navbar
        _("Bug Report"),
        _("Courses"),
    ]
