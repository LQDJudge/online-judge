import errno
import os
from zipfile import BadZipFile, ZipFile

from django.core.validators import FileExtensionValidator
from django.core.cache import cache
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError

from judge.utils.problem_data import ProblemDataStorage, get_file_cachekey

__all__ = [
    "problem_data_storage",
    "problem_directory_file",
    "ProblemData",
    "ProblemTestCase",
    "CHECKERS",
]

problem_data_storage = ProblemDataStorage()


def problem_directory_file_helper(code, filename):
    return os.path.join(code, os.path.basename(filename))


def problem_directory_file(data, filename):
    return problem_directory_file_helper(data.problem.code, filename)


CHECKERS = (
    ("standard", _("Standard")),
    ("floats", _("Floats")),
    ("floatsabs", _("Floats (absolute)")),
    ("floatsrel", _("Floats (relative)")),
    ("rstripped", _("Non-trailing spaces")),
    ("sorted", _("Unordered")),
    ("identical", _("Byte identical")),
    ("linecount", _("Line-by-line")),
    ("custom", _("Custom checker (PY)")),
    ("customcpp", _("Custom checker (CPP)")),
    ("interact", _("Interactive")),
    ("testlib", _("Testlib")),
)


class ProblemData(models.Model):
    problem = models.OneToOneField(
        "Problem",
        verbose_name=_("problem"),
        related_name="data_files",
        on_delete=models.CASCADE,
    )
    zipfile = models.FileField(
        verbose_name=_("data zip file"),
        storage=problem_data_storage,
        null=True,
        blank=True,
        upload_to=problem_directory_file,
    )
    generator = models.FileField(
        verbose_name=_("generator file"),
        storage=problem_data_storage,
        null=True,
        blank=True,
        upload_to=problem_directory_file,
    )
    output_prefix = models.IntegerField(
        verbose_name=_("output prefix length"), blank=True, null=True
    )
    output_limit = models.IntegerField(
        verbose_name=_("output limit length"), blank=True, null=True
    )
    feedback = models.TextField(
        verbose_name=_("init.yml generation feedback"), blank=True
    )
    checker = models.CharField(
        max_length=10, verbose_name=_("checker"), choices=CHECKERS, blank=True
    )
    checker_args = models.TextField(
        verbose_name=_("checker arguments"),
        blank=True,
        help_text=_("checker arguments as a JSON object"),
    )
    custom_checker = models.FileField(
        verbose_name=_("custom checker file"),
        storage=problem_data_storage,
        null=True,
        blank=True,
        upload_to=problem_directory_file,
        validators=[FileExtensionValidator(allowed_extensions=["py"])],
    )
    custom_checker_cpp = models.FileField(
        verbose_name=_("custom cpp checker file"),
        storage=problem_data_storage,
        null=True,
        blank=True,
        upload_to=problem_directory_file,
        validators=[FileExtensionValidator(allowed_extensions=["cpp"])],
    )
    interactive_judge = models.FileField(
        verbose_name=_("interactive judge"),
        storage=problem_data_storage,
        null=True,
        blank=True,
        upload_to=problem_directory_file,
        validators=[FileExtensionValidator(allowed_extensions=["cpp"])],
    )
    fileio_input = models.TextField(
        verbose_name=_("input file name"),
        blank=True,
        null=True,
        help_text=_("Leave empty for stdin"),
    )
    fileio_output = models.TextField(
        verbose_name=_("output file name"),
        blank=True,
        null=True,
        help_text=_("Leave empty for stdout"),
    )
    output_only = models.BooleanField(
        verbose_name=_("is output only"),
        help_text=_("Support output-only problem"),
        null=True,
    )
    use_ioi_signature = models.BooleanField(
        verbose_name=_("is IOI signature"),
        help_text=_("Use IOI Signature"),
        null=True,
    )
    signature_handler = models.FileField(
        verbose_name=_("signature handler"),
        storage=problem_data_storage,
        null=True,
        blank=True,
        upload_to=problem_directory_file,
        validators=[FileExtensionValidator(allowed_extensions=["cpp"])],
    )
    signature_header = models.FileField(
        verbose_name=_("signature header"),
        storage=problem_data_storage,
        null=True,
        blank=True,
        upload_to=problem_directory_file,
        validators=[FileExtensionValidator(allowed_extensions=["h"])],
    )

    __original_zipfile = None

    def __init__(self, *args, **kwargs):
        super(ProblemData, self).__init__(*args, **kwargs)
        self.__original_zipfile = self.zipfile

    def save(self, *args, **kwargs):
        # Delete caches
        if self.__original_zipfile:
            try:
                files = ZipFile(self.__original_zipfile.path).namelist()
                for file in files:
                    cache_key = "problem_archive:%s:%s" % (
                        self.problem.code,
                        get_file_cachekey(file),
                    )
                    cache.delete(cache_key)
            except (BadZipFile, FileNotFoundError):
                pass
            if self.zipfile != self.__original_zipfile:
                self.__original_zipfile.delete(save=False)
        return super(ProblemData, self).save(*args, **kwargs)

    def has_yml(self):
        return problem_data_storage.exists("%s/init.yml" % self.problem.code)

    def _update_code(self, original, new):
        if self.zipfile:
            self.zipfile.name = problem_directory_file_helper(new, self.zipfile.name)
        if self.generator:
            self.generator.name = problem_directory_file_helper(
                new, self.generator.name
            )
        if self.custom_checker:
            self.custom_checker.name = problem_directory_file_helper(
                new, self.custom_checker.name
            )
        if self.custom_checker:
            self.custom_checker.name = problem_directory_file_helper(
                new, self.custom_checker.name
            )
        if self.custom_checker_cpp:
            self.custom_checker_cpp.name = problem_directory_file_helper(
                new, self.custom_checker_cpp.name
            )
        if self.interactive_judge:
            self.interactive_judge.name = problem_directory_file_helper(
                new, self.interactive_judge.name
            )
        if self.signature_header:
            self.signature_header.name = problem_directory_file_helper(
                new, self.signature_header.name
            )
        if self.signature_handler:
            self.signature_handler.name = problem_directory_file_helper(
                new, self.signature_handler.name
            )
        for grader in self.problem.signature_graders.all():
            if grader.handler:
                grader.handler.name = problem_directory_file_helper(
                    new, grader.handler.name
                )
            if grader.header:
                grader.header.name = problem_directory_file_helper(
                    new, grader.header.name
                )
            grader.save()

        self.save()

    _update_code.alters_data = True


class ProblemTestCase(models.Model):
    dataset = models.ForeignKey(
        "Problem",
        verbose_name=_("problem data set"),
        related_name="cases",
        on_delete=models.CASCADE,
    )
    order = models.IntegerField(verbose_name=_("case position"))
    type = models.CharField(
        max_length=1,
        verbose_name=_("case type"),
        choices=(
            ("C", _("Normal case")),
            ("S", _("Batch start")),
            ("E", _("Batch end")),
        ),
        default="C",
    )
    input_file = models.CharField(
        max_length=100, verbose_name=_("input file name"), blank=True
    )
    output_file = models.CharField(
        max_length=100, verbose_name=_("output file name"), blank=True
    )
    generator_args = models.TextField(verbose_name=_("generator arguments"), blank=True)
    points = models.IntegerField(verbose_name=_("point value"), blank=True, null=True)
    is_pretest = models.BooleanField(verbose_name=_("case is pretest?"))
    output_prefix = models.IntegerField(
        verbose_name=_("output prefix length"), blank=True, null=True
    )
    output_limit = models.IntegerField(
        verbose_name=_("output limit length"), blank=True, null=True
    )
    checker = models.CharField(
        max_length=10, verbose_name=_("checker"), choices=CHECKERS, blank=True
    )
    checker_args = models.TextField(
        verbose_name=_("checker arguments"),
        blank=True,
        help_text=_("checker arguments as a JSON object"),
    )


class ProblemSignatureGrader(models.Model):
    problem = models.ForeignKey(
        "Problem",
        related_name="signature_graders",
        on_delete=models.CASCADE,
    )
    language = models.CharField(
        max_length=10,
        choices=(
            ("c", "C/C++"),
            ("java", "Java"),
            ("python", "Python"),
        ),
        verbose_name=_("signature language"),
    )
    handler = models.FileField(
        verbose_name=_("signature handler"),
        storage=problem_data_storage,
        upload_to=problem_directory_file,
        validators=[
            FileExtensionValidator(allowed_extensions=["cpp", "c", "java", "py"])
        ],
    )
    header = models.FileField(
        verbose_name=_("signature header"),
        storage=problem_data_storage,
        upload_to=problem_directory_file,
        validators=[FileExtensionValidator(allowed_extensions=["h"])],
        null=True,
        blank=True,
    )
