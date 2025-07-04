import hashlib
import json
import os
import re
import yaml
import zipfile
import shutil

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.urls import reverse
from django.utils.translation import gettext as _
from django.core.cache import cache

from judge.logging import log_exception

if os.altsep:

    def split_path_first(
        path, repath=re.compile("[%s]" % re.escape(os.sep + os.altsep))
    ):
        return repath.split(path, 1)

else:

    def split_path_first(path):
        return path.split(os.sep, 1)


class ProblemDataStorage(FileSystemStorage):
    def __init__(self):
        super(ProblemDataStorage, self).__init__(settings.DMOJ_PROBLEM_DATA_ROOT)

    def url(self, name):
        path = split_path_first(name)
        if len(path) != 2:
            raise ValueError("This file is not accessible via a URL.")
        return reverse("problem_data_file", args=path)

    def _save(self, name, content):
        if self.exists(name):
            self.delete(name)
        return super(ProblemDataStorage, self)._save(name, content)

    def get_available_name(self, name, max_length=None):
        return name

    def rename(self, old, new):
        return os.rename(self.path(old), self.path(new))

    def delete_directory(self, name):
        directory_path = self.path(name)
        try:
            shutil.rmtree(directory_path)
        except FileNotFoundError:
            pass


class ProblemDataError(Exception):
    def __init__(self, message):
        super(ProblemDataError, self).__init__(message)
        self.message = message


class ProblemDataCompiler(object):
    def __init__(self, problem, data, cases, files):
        self.problem = problem
        self.data = data
        self.cases = cases
        self.files = files

        self.generator = data.generator

    def make_init(self):
        cases = []
        batch = None

        def end_batch():
            if not batch["batched"]:
                raise ProblemDataError(_("Empty batches not allowed."))
            cases.append(batch)

        def make_checker(case):
            if case.checker == "custom":
                custom_checker_path = split_path_first(case.custom_checker.name)
                if len(custom_checker_path) != 2:
                    raise ProblemDataError(
                        _("How did you corrupt the custom checker path?")
                    )
                return custom_checker_path[1]

            latest_cpp_key = _get_latest_cpp_key()

            if case.checker == "customcpp":
                custom_checker_path = split_path_first(case.custom_checker_cpp.name)
                if len(custom_checker_path) != 2:
                    raise ProblemDataError(
                        _("How did you corrupt the custom checker path?")
                    )
                return {
                    "name": "bridged",
                    "args": {
                        "files": custom_checker_path[1],
                        "lang": latest_cpp_key,
                        "type": "lqdoj",
                    },
                }

            if case.checker == "testlib":
                custom_checker_path = split_path_first(case.custom_checker_cpp.name)
                if len(custom_checker_path) != 2:
                    raise ProblemDataError(
                        _("How did you corrupt the custom checker path?")
                    )
                return {
                    "name": "bridged",
                    "args": {
                        "files": custom_checker_path[1],
                        "lang": latest_cpp_key,
                        "type": "testlib",
                    },
                }

            if case.checker_args:
                return {
                    "name": case.checker,
                    "args": json.loads(case.checker_args),
                }
            return case.checker

        for i, case in enumerate(self.cases, 1):
            if case.type == "C":
                data = {}
                if batch:
                    if case.points is None:
                        case.points = 0
                    case.is_pretest = batch["is_pretest"]
                else:
                    if case.points is None:
                        raise ProblemDataError(
                            _("Points must be defined for non-batch case #%d.") % i
                        )
                    data["is_pretest"] = case.is_pretest

                if not self.generator:
                    if case.input_file not in self.files:
                        raise ProblemDataError(
                            _("Input file for case %d does not exist: %s")
                            % (i, case.input_file)
                        )
                    if case.output_file not in self.files:
                        raise ProblemDataError(
                            _("Output file for case %d does not exist: %s")
                            % (i, case.output_file)
                        )

                if case.input_file:
                    data["in"] = case.input_file
                if case.output_file:
                    data["out"] = case.output_file
                if case.points is not None:
                    data["points"] = case.points
                if case.generator_args:
                    data["generator_args"] = case.generator_args.split()
                if case.output_limit is not None:
                    data["output_limit_length"] = case.output_limit
                if case.output_prefix is not None:
                    data["output_prefix_length"] = case.output_prefix
                if case.checker:
                    data["checker"] = make_checker(case)
                else:
                    case.checker_args = ""
                case.save(update_fields=("checker_args", "is_pretest"))
                (batch["batched"] if batch else cases).append(data)
            elif case.type == "S":
                if batch:
                    end_batch()
                if case.points is None:
                    raise ProblemDataError(
                        _("Batch start case #%d requires points.") % i
                    )
                batch = {
                    "points": case.points,
                    "batched": [],
                    "is_pretest": case.is_pretest,
                }
                if case.generator_args:
                    batch["generator_args"] = case.generator_args.split()
                if case.output_limit is not None:
                    batch["output_limit_length"] = case.output_limit
                if case.output_prefix is not None:
                    batch["output_prefix_length"] = case.output_prefix
                if case.checker:
                    batch["checker"] = make_checker(case)
                else:
                    case.checker_args = ""
                case.input_file = ""
                case.output_file = ""
                case.save(update_fields=("checker_args", "input_file", "output_file"))
            elif case.type == "E":
                if not batch:
                    raise ProblemDataError(
                        _("Attempt to end batch outside of one in case #%d") % i
                    )
                case.is_pretest = batch["is_pretest"]
                case.input_file = ""
                case.output_file = ""
                case.generator_args = ""
                case.checker = ""
                case.checker_args = ""
                case.save()
                end_batch()
                batch = None
        if batch:
            end_batch()

        init = {}

        if self.data.zipfile:
            zippath = split_path_first(self.data.zipfile.name)
            if len(zippath) != 2:
                raise ProblemDataError(_("How did you corrupt the zip path?"))
            init["archive"] = zippath[1]

        if self.generator:
            generator_path = split_path_first(self.generator.name)
            if len(generator_path) != 2:
                raise ProblemDataError(_("How did you corrupt the generator path?"))
            init["generator"] = generator_path[1]

        pretests = [case for case in cases if case["is_pretest"]]
        for case in cases:
            del case["is_pretest"]
        if pretests:
            init["pretest_test_cases"] = pretests
        if cases:
            init["test_cases"] = cases
        if self.data.output_limit is not None:
            init["output_limit_length"] = self.data.output_limit
        if self.data.output_prefix is not None:
            init["output_prefix_length"] = self.data.output_prefix
        if self.data.checker:
            if self.data.checker in ("interact", "interacttl"):
                interactor_path = split_path_first(self.data.interactive_judge.name)
                if len(interactor_path) != 2:
                    raise ProblemDataError(_("Invalid interactor judge"))
                init["interactive"] = {
                    "files": interactor_path[1],
                    "feedback": True,
                    "type": "lqdoj" if self.data.checker == "interact" else "testlib",
                }
                init["unbuffered"] = True
            else:
                init["checker"] = make_checker(self.data)
        else:
            self.data.checker_args = ""
        if self.data.fileio_input:
            if "file_io" not in init:
                init["file_io"] = {}
            init["file_io"]["input"] = self.data.fileio_input
        if self.data.fileio_output:
            if "file_io" not in init:
                init["file_io"] = {}
            init["file_io"]["output"] = self.data.fileio_output
        if self.data.output_only:
            init["output_only"] = True
        if self.data.use_ioi_signature:
            signature_graders = {}
            for grader in self.problem.signature_graders.all():
                handler_path = split_path_first(grader.handler.name)
                if len(handler_path) != 2:
                    raise ProblemDataError(_("Invalid signature handler"))

                grader_info = {
                    "entry": handler_path[1],
                }

                if grader.language == "c":
                    header_path = split_path_first(grader.header.name)
                    if len(header_path) != 2:
                        raise ProblemDataError(_("Invalid signature header for C/C++"))
                    grader_info["header"] = header_path[1]
                    signature_graders.update(grader_info)
                else:
                    signature_graders[grader.language] = grader_info

            init["signature_grader"] = signature_graders

        return init

    def compile(self):
        from judge.models import problem_data_storage

        yml_file = "%s/init.yml" % self.problem.code
        try:
            init = yaml.safe_dump(self.make_init())
        except ProblemDataError as e:
            self.data.feedback = e.message
            self.data.save()
            problem_data_storage.delete(yml_file)
        else:
            self.data.feedback = ""
            self.data.save()
            problem_data_storage.save(yml_file, ContentFile(init))

    @classmethod
    def generate(cls, *args, **kwargs):
        self = cls(*args, **kwargs)
        self.compile()


def get_visible_content(data):
    data = data or b""
    data = data.replace(b"\r\n", b"\r").replace(b"\r", b"\n")

    data = data.decode("utf-8")

    if len(data) > settings.TESTCASE_VISIBLE_LENGTH:
        data = data[: settings.TESTCASE_VISIBLE_LENGTH]
        data += "." * 3
    return data


def get_file_cachekey(file):
    return hashlib.sha1(file.encode()).hexdigest()


def get_problem_case(problem, files):
    result = {}
    uncached_files = []

    for file in files:
        cache_key = "problem_archive:%s:%s" % (problem.code, get_file_cachekey(file))
        qs = cache.get(cache_key)
        if qs is None:
            uncached_files.append(file)
        else:
            result[file] = qs

    if not uncached_files:
        return result

    archive_path = os.path.join(
        settings.DMOJ_PROBLEM_DATA_ROOT, str(problem.data_files.zipfile)
    )
    if not os.path.exists(archive_path):
        log_exception('archive file "%s" does not exist' % archive_path)
        return {}
    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except zipfile.BadZipfile:
        log_exception('bad archive: "%s"' % archive_path)
        return {}

    for file in uncached_files:
        cache_key = "problem_archive:%s:%s" % (problem.code, get_file_cachekey(file))
        with archive.open(file) as f:
            s = f.read(settings.TESTCASE_VISIBLE_LENGTH + 3)
            # add this so there are no characters left behind (ex, 'á' = 2 utf-8 chars)
            while True:
                try:
                    s.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    next_char = f.read(1)
                    if next_char:
                        s += next_char
                    else:
                        s = f"File {file} is not able to decode in utf-8"
                        s = s.encode("utf-8")
                        break
            qs = get_visible_content(s)
        cache.set(cache_key, qs, 86400)
        result[file] = qs

    return result


def _get_latest_cpp_key():
    from judge.models import Language

    cpp_keys = ["CPP20", "CPP17", "CPP14", "CPP11"]

    language_keys = list(
        Language.objects.filter(key__in=cpp_keys).values_list("key", flat=True)
    )

    for key in cpp_keys:
        if key in language_keys:
            return key

    return None
