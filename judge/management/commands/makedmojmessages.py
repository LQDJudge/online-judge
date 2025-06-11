import glob
import io
import os
import sys
from pathlib import Path

from django.conf import settings
from django.core.management import CommandError
from django.core.management.commands.makemessages import (
    Command as MakeMessagesCommand,
    check_programs,
    normalize_eols,
)
from django.core.management.utils import popen_wrapper

from judge.models import NavigationBar, ProblemType


class Command(MakeMessagesCommand):
    def add_arguments(self, parser):
        parser.add_argument(
            "--locale",
            "-l",
            default=[],
            dest="locale",
            action="append",
            help="Creates or updates the message files for the given locale(s) (e.g. pt_BR). "
            "Can be used multiple times.",
        )
        parser.add_argument(
            "--exclude",
            "-x",
            default=[],
            dest="exclude",
            action="append",
            help="Locales to exclude. Default is none. Can be used multiple times.",
        )
        parser.add_argument(
            "--all",
            "-a",
            action="store_true",
            dest="all",
            default=False,
            help="Updates the message files for all existing locales.",
        )
        parser.add_argument(
            "--no-wrap",
            action="store_true",
            dest="no_wrap",
            default=False,
            help="Don't break long message lines into several lines.",
        )
        parser.add_argument(
            "--remove-obsolete",
            action="store_true",
            dest="remove_obsolete",
            default=False,
            help="Remove obsolete message strings.",
        )
        parser.add_argument(
            "--keep-pot",
            action="store_true",
            dest="keep_pot",
            default=False,
            help="Keep .pot file after making messages. Useful when debugging.",
        )
        parser.add_argument(
            "--no-mark-obsolete",
            action="store_false",
            dest="mark_obsolete",
            default=True,
            help="Keep obsolete strings uncommented (without #~ prefix). By default, obsolete strings are marked with #~.",
        )

    def handle(self, *args, **options):
        locale = options.get("locale")
        exclude = options.get("exclude")
        self.domain = "dmoj-user"
        self.verbosity = options.get("verbosity")
        process_all = options.get("all")

        # Need to ensure that the i18n framework is enabled
        if settings.configured:
            settings.USE_I18N = True
        else:
            settings.configure(USE_I18N=True)

        # Avoid messing with mutable class variables
        if options.get("no_wrap"):
            self.msgmerge_options = self.msgmerge_options[:] + ["--no-wrap"]
            self.msguniq_options = self.msguniq_options[:] + ["--no-wrap"]
            self.msgattrib_options = self.msgattrib_options[:] + ["--no-wrap"]
            self.xgettext_options = self.xgettext_options[:] + ["--no-wrap"]
        if options.get("no_location"):
            self.msgmerge_options = self.msgmerge_options[:] + ["--no-location"]
            self.msguniq_options = self.msguniq_options[:] + ["--no-location"]
            self.msgattrib_options = self.msgattrib_options[:] + ["--no-location"]
            self.xgettext_options = self.xgettext_options[:] + ["--no-location"]

        # Handle obsolete string behavior
        # By default, keep obsolete strings marked with #~
        # Remove them completely only if --remove-obsolete is explicitly specified
        self.mark_obsolete = options.get("mark_obsolete")

        self.remove_obsolete = options.get("remove_obsolete")
        self.keep_pot = options.get("keep_pot")

        if locale is None and not exclude and not process_all:
            raise CommandError(
                "Type '%s help %s' for usage information."
                % (os.path.basename(sys.argv[0]), sys.argv[1])
            )

        self.invoked_for_django = False
        self.locale_paths = []
        self.default_locale_path = None
        if os.path.isdir(os.path.join("conf", "locale")):
            self.locale_paths = [os.path.abspath(os.path.join("conf", "locale"))]
            self.default_locale_path = self.locale_paths[0]
            self.invoked_for_django = True
        else:
            self.locale_paths.extend(settings.LOCALE_PATHS)
            # Allow to run makemessages inside an app dir
            if os.path.isdir("locale"):
                self.locale_paths.append(os.path.abspath("locale"))
            if self.locale_paths:
                self.default_locale_path = self.locale_paths[0]
                if not os.path.exists(self.default_locale_path):
                    os.makedirs(self.default_locale_path)

        # Build locale list
        locale_dirs = list(
            filter(os.path.isdir, glob.glob("%s/*" % self.default_locale_path))
        )
        all_locales = list(map(os.path.basename, locale_dirs))

        # Account for excluded locales
        if process_all:
            locales = all_locales
        else:
            locales = locale or all_locales
            locales = set(locales) - set(exclude)

        if locales:
            check_programs("msguniq", "msgmerge", "msgattrib")

        check_programs("xgettext")

        try:
            potfiles = self.build_potfiles()

            # Build po files for each selected locale
            for locale in locales:
                if self.verbosity > 0:
                    self.stdout.write("processing locale %s\n" % locale)
                for potfile in potfiles:
                    self.write_po_file(potfile, locale)
        finally:
            if not self.keep_pot:
                self.remove_potfiles()

    def find_files(self, root):
        return []

    def _emit_message(self, potfile, string):
        potfile.write(
            """
msgid "%s"
msgstr ""
"""
            % string.replace("\\", r"\\")
            .replace("\t", "\\t")
            .replace("\n", "\\n")
            .replace('"', '\\"')
        )

    def process_files(self, file_list):
        with io.open(
            os.path.join(self.default_locale_path, "dmoj-user.pot"),
            "w",
            encoding="utf-8",
        ) as potfile:
            potfile.write(
                """
msgid ""
msgstr ""

"Content-Type: text/plain; charset=utf-8\\n"
                """
            )
            if self.verbosity > 1:
                self.stdout.write("processing navigation bar")
            for label in NavigationBar.objects.values_list("label", flat=True):
                if self.verbosity > 2:
                    self.stdout.write('processing navigation item label "%s"\n' % label)
                self._emit_message(potfile, label)

            if self.verbosity > 1:
                self.stdout.write("processing problem types")
            for name in ProblemType.objects.values_list("full_name", flat=True):
                if self.verbosity > 2:
                    self.stdout.write('processing problem type name "%s"\n' % name)
                self._emit_message(potfile, name)

    def write_po_file(self, potfile, locale):
        """
        Create or update the PO file for self.domain and `locale`.
        Use contents of the existing `potfile`.

        Override Django's default behavior to handle obsolete strings differently:
        - By default: keep obsolete strings marked with #~ (new default behavior)
        - With --remove-obsolete: remove obsolete strings completely
        """
        basedir = os.path.join(os.path.dirname(potfile), locale, "LC_MESSAGES")
        os.makedirs(basedir, exist_ok=True)
        pofile = os.path.join(basedir, "%s.po" % self.domain)

        if os.path.exists(pofile):
            args = ["msgmerge"] + self.msgmerge_options + [pofile, potfile]
            _, errors, status = popen_wrapper(args)
            if errors:
                if status != 0:  # STATUS_OK
                    raise CommandError(
                        "errors happened while running msgmerge\n%s" % errors
                    )
                elif self.verbosity > 0:
                    self.stdout.write(errors)
            msgs = Path(pofile).read_text(encoding="utf-8")
        else:
            with open(potfile, encoding="utf-8") as fp:
                msgs = fp.read()
            if not self.invoked_for_django:
                msgs = self.copy_plural_forms(msgs, locale)

        msgs = normalize_eols(msgs)
        msgs = msgs.replace(
            "#. #-#-#-#-#  %s.pot (PACKAGE VERSION)  #-#-#-#-#\n" % self.domain, ""
        )
        with open(pofile, "w", encoding="utf-8") as fp:
            fp.write(msgs)

        # Handle obsolete strings based on flags
        if self.remove_obsolete:
            # --remove-obsolete flag: remove obsolete strings completely
            args = ["msgattrib"] + self.msgattrib_options + ["-o", pofile, pofile]
            msgs, errors, status = popen_wrapper(args)
            if errors:
                if status != 0:  # STATUS_OK
                    raise CommandError(
                        "errors happened while running msgattrib\n%s" % errors
                    )
                elif self.verbosity > 0:
                    self.stdout.write(errors)
        elif not self.mark_obsolete:
            # --no-mark-obsolete flag: keep obsolete strings but remove #~ markers
            with open(pofile, "r", encoding="utf-8") as fp:
                content = fp.read()

            # Remove #~ markers from obsolete entries
            lines = content.split("\n")
            processed_lines = []
            for line in lines:
                if line.startswith("#~ "):
                    # Remove the #~ prefix but keep the content
                    processed_lines.append(line[3:])
                else:
                    processed_lines.append(line)

            with open(pofile, "w", encoding="utf-8") as fp:
                fp.write("\n".join(processed_lines))
        # By default (mark_obsolete=True), we keep obsolete strings marked with #~
