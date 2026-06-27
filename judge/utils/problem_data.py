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
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
import logging

from judge.logging import log_exception

debug_log = logging.getLogger("judge.debug")

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
            # File-bearing checker types (custom, customcpp, testlib, testlibcms)
            # store their file on `ProblemData`, not on `ProblemTestCase`. If a
            # per-case row sets one of these, fall back to its checker_args
            # JSON below — we can't resolve a path to a per-case file because
            # the model doesn't carry one.
            if not hasattr(case, "custom_checker") and case.checker in (
                "custom",
                "customcpp",
                "testlib",
                "testlibcms",
            ):
                raise ProblemDataError(
                    _(
                        "Per-case checker %s requires a file that's only "
                        "configurable on the problem (not per test case). "
                        "Set the checker at the problem level instead."
                    )
                    % case.checker
                )

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

            if case.checker in ("testlib", "testlibcms"):
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
                        # `testlibcms` uses the CMS-style testlib fork (Kian Mirjalali) which
                        # expects argv `input answer output` and prints CMS-format scores.
                        "type": "cms" if case.checker == "testlibcms" else "testlib",
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
                if case.points is None:
                    raise ProblemDataError(
                        _("Points must be defined for case #%d.") % i
                    )
                if batch:
                    case.is_pretest = batch["is_pretest"]
                else:
                    data["is_pretest"] = case.is_pretest

                if not self.generator:
                    if case.input_file not in self.files:
                        raise ProblemDataError(
                            _(
                                "Input file for case %(case_num)d does not exist: %(filename)s"
                            )
                            % {"case_num": i, "filename": case.input_file}
                        )
                    if case.output_file not in self.files:
                        raise ProblemDataError(
                            _(
                                "Output file for case %(case_num)d does not exist: %(filename)s"
                            )
                            % {"case_num": i, "filename": case.output_file}
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
                # Emit score_type when non-default ("min") so the judge
                # knows how to aggregate per-case scores into the batch score.
                # Default "sum" needs no entry (judge defaults to sum).
                if case.batch_scoring == "min":
                    batch["score_type"] = "min"
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
            elif (
                self.data.checker in ("testlib", "testlibcms")
                and not self.data.custom_checker_cpp
            ):
                # Communication tasks may set data.checker = "testlibcms" purely as a
                # type signal (the manager scores itself; no separate checker file).
                pass
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
        if self.data.binary_data:
            init["binary_data"] = True
        if self.data.output_zip_size_mb:
            init["fize_size_limit"] = self.data.output_zip_size_mb
        if self.data.testcase_validator:
            validator_path = split_path_first(self.data.testcase_validator.name)
            if len(validator_path) != 2:
                raise ProblemDataError(_("Invalid validator source path"))
            filename = validator_path[1]
            ext = os.path.splitext(filename)[1].lower()
            if ext == ".cpp":
                lang = _get_latest_cpp_key()
            elif ext == ".py":
                lang = "PY3"
            else:
                raise ProblemDataError(_("Unsupported validator extension: %s") % ext)
            init["validator"] = {
                "source": filename,
                "language": lang,
            }

        # Communication tasks (IOI-style separate manager process) take
        # precedence over the plain signature_grader emit: the user binary is
        # still compiled with stub.cpp + <task>.h (provided by the C signature
        # grader rows), but the judge launches `num_processes` copies of it
        # alongside a sandboxed manager binary, talking over FIFOs.
        if (
            self.data.communication_manager
            and (self.data.communication_num_processes or 0) >= 1
        ):
            mgr_path = split_path_first(self.data.communication_manager.name)
            if len(mgr_path) != 2:
                raise ProblemDataError(_("Invalid communication manager"))
            communication = {
                "manager": {
                    "files": mgr_path[1],
                    "lang": _get_latest_cpp_key(),
                },
                "num_processes": int(self.data.communication_num_processes),
                # IOI/CMS managers print a score to stdout and always exit 0;
                # other managers signal pass/fail via exit code.
                "type": "cms" if self.data.checker == "testlibcms" else "default",
            }
            # Reuse the C/C++ signature grader as the stub + header.
            if self.data.use_ioi_signature:
                for grader in self.problem.signature_graders.all():
                    if grader.language != "c":
                        continue
                    handler_path = split_path_first(grader.handler.name)
                    header_path = split_path_first(grader.header.name)
                    if len(handler_path) == 2 and len(header_path) == 2:
                        communication["signature"] = {
                            "entry": handler_path[1],
                            "header": header_path[1],
                        }
                    break
            init["communication"] = communication
        elif self.data.use_ioi_signature:
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
            self._notify_judges()

    def _notify_judges(self):
        """Notify connected judges that problem data has changed.

        Gated behind DMOJ_PROBLEM_DATA_PUSH_UPDATE setting (default False).
        Set to True if you want bridge push notifications in addition to
        watchdog monitoring.
        """
        if not getattr(settings, "DMOJ_PROBLEM_DATA_PUSH_UPDATE", False):
            return
        from judge.judgeapi import notify_problem_update

        try:
            notify_problem_update()
        except Exception:
            pass  # Non-critical: logged in notify_problem_update

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
    unique_files = list(dict.fromkeys(files))
    if not unique_files:
        return result

    file_to_key = {
        file: "problem_archive:%s:%s" % (problem.code, get_file_cachekey(file))
        for file in unique_files
    }
    cached = cache.get_many(list(file_to_key.values()))

    uncached_files = []
    for file, key in file_to_key.items():
        if key in cached:
            result[file] = cached[key]
        else:
            uncached_files.append(file)

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

    to_set = {}
    for file in uncached_files:
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
        to_set[file_to_key[file]] = qs
        result[file] = qs

    if to_set:
        cache.set_many(to_set, 86400)

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


def notify_problem_authors(
    problem,
    error_message,
    error_type="Checker Error",
    submission=None,
    fallback_to_admin=True,
):
    """
    Send email notification to problem authors/curators when there's a checker error.

    Args:
        problem: Problem instance
        error_message: Error message to include in email
        error_type: Type of error (default: "Checker Error")
        submission: Submission instance that caused the error (optional)
        fallback_to_admin: Whether to email admins when no owner email is available.
    """
    if not problem:
        if fallback_to_admin:
            log_exception(f"Problem Unknown {error_type}: {error_message}")
        return

    owner_ids = set(problem.authors.values_list("id", flat=True))
    owner_ids.update(problem.curators.values_list("id", flat=True))

    owner_emails = []
    seen_emails = set()
    for owner in problem.authors.model.get_cached_instances(*owner_ids):
        email = owner.get_email()
        if email and email not in seen_emails:
            owner_emails.append(email)
            seen_emails.add(email)

    if not owner_emails:
        if fallback_to_admin:
            # Fallback to admin notification if no editable owner can receive email.
            log_exception(f"Problem {problem.code} {error_type}: {error_message}")
        else:
            debug_log.info(
                "Skipped admin email fallback for %s %s; in-app owner notification sent",
                problem.code,
                error_type,
            )
        return

    # Email throttling - check cache to prevent spam
    throttle_key = f"problem_author_email_throttle:{problem.code}:{hash(error_message)}"

    # Check if we've already sent this error notification recently (within 1 hour)
    if cache.get(throttle_key):
        debug_log.info(f"Email throttled for problem {problem.code}: {error_type}")
        return

    # Set throttle cache for 1 hour
    cache.set(throttle_key, True, 3600)

    # Prepare email content
    subject = f"[LQDOJ] {error_type} in Problem {problem.code}"

    context = {
        "problem": problem,
        "error_message": error_message,
        "error_type": error_type,
        "site_name": getattr(settings, "SITE_NAME", "LQDOJ"),
        "problem_url": f"{getattr(settings, 'SITE_DOMAIN', '')}/problem/{problem.code}",
        "edit_url": f"{getattr(settings, 'SITE_DOMAIN', '')}/problem/{problem.code}/test_data",
        "protocol": "http",
        "domain": getattr(settings, "SITE_DOMAIN", "")
        .replace("http://", "")
        .replace("https://", ""),
    }

    # Add submission URL if submission is provided
    if submission:
        context["submission_url"] = (
            f"{getattr(settings, 'SITE_DOMAIN', '')}/submission/{submission.id}"
        )

    # Create email body
    html_message = render_to_string("judge/emails/problem_checker_error.html", context)
    plain_message = strip_tags(html_message)

    try:
        send_mail(
            subject=subject,
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=owner_emails,
            html_message=html_message,
            fail_silently=False,
        )

        # Log successful notification
        debug_log.info(
            f"Notified problem authors for {problem.code}: {', '.join(owner_emails)}"
        )

    except Exception as e:
        # If email fails, fall back to admin notification
        log_exception(f"Failed to notify problem authors for {problem.code}: {str(e)}")
        log_exception(f"Original error - {error_type}: {error_message}")
