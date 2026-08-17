import json
import os
import shlex
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from django.http import Http404


def build_generator_static_package(problem, data, cases):
    generator_name = os.path.basename(data.generator.name) or "generator.cpp"
    try:
        with data.generator.open("rb") as f:
            generator_source = f.read()
    except FileNotFoundError:
        raise Http404()

    package = BytesIO()
    with ZipFile(package, "w", ZIP_DEFLATED) as zf:
        zf.writestr("README.md", _readme(problem, generator_name, len(cases)))
        zf.writestr(generator_name, generator_source)
        zf.writestr("cases.tsv", _cases_tsv(cases))
        zf.writestr("cases.json", _cases_json(cases))
        zf.writestr("generate.py", _python_runner(generator_name))
        zf.writestr("generate.sh", _shell_runner(generator_name))
        zf.writestr("generate.bat", _batch_runner(generator_name))

    package.seek(0)
    return package.getvalue()


def _readme(problem, generator_name, case_count):
    return (
        "# Static test materializer for {name}\n\n"
        "This package generates static input/output files from the problem's C++ "
        "test generator and saved generator arguments.\n\n"
        "What it creates:\n\n"
        "- `tests/001.in`, `tests/001.out`, ...\n"
        "- `static_tests.zip`, ready to upload as the problem data ZIP\n\n"
        "Requirements:\n\n"
        "- A C++ compiler available as `g++`.\n"
        "- Optional: Python 3 for `generate.py`.\n\n"
        "Recommended:\n\n"
        "```bash\n"
        "python3 generate.py\n"
        "```\n\n"
        "Without Python on macOS/Linux:\n\n"
        "```bash\n"
        "bash generate.sh\n"
        "```\n\n"
        "Without Python on Windows:\n\n"
        "```bat\n"
        "generate.bat\n"
        "```\n\n"
        "After `static_tests.zip` is created, upload it in the Test Data page, "
        "replace generated rows with file-backed rows using matching `.in/.out` "
        "files, then click Apply.\n\n"
        "Generator file: `{generator}`\n\n"
        "Case count: {case_count}\n\n"
        "---\n\n"
        "# Gói sinh test tĩnh cho {name}\n\n"
        "Gói này tạo các file input/output tĩnh từ generator C++ của bài và "
        "các dòng tham số generator đã lưu.\n\n"
        "Kết quả tạo ra:\n\n"
        "- `tests/001.in`, `tests/001.out`, ...\n"
        "- `static_tests.zip`, có thể upload lại vào mục Data zip file\n\n"
        "Yêu cầu:\n\n"
        "- Có trình biên dịch C++ chạy được bằng lệnh `g++`.\n"
        "- Nếu có Python 3, nên dùng `generate.py`.\n\n"
        "Khuyến nghị:\n\n"
        "```bash\n"
        "python3 generate.py\n"
        "```\n\n"
        "Nếu không có Python trên macOS/Linux:\n\n"
        "```bash\n"
        "bash generate.sh\n"
        "```\n\n"
        "Nếu không có Python trên Windows:\n\n"
        "```bat\n"
        "generate.bat\n"
        "```\n\n"
        "Sau khi tạo xong `static_tests.zip`, upload file này trong trang Test "
        "Data, đổi các dòng test đang dùng generator sang các file `.in/.out` "
        "tương ứng, rồi bấm Apply.\n\n"
        "File generator: `{generator}`\n\n"
        "Số test: {case_count}\n"
    ).format(
        name=problem.name,
        generator=generator_name,
        case_count=case_count,
    )


def _cases_tsv(cases):
    lines = ["# name\tgenerator args"]
    for index, case in enumerate(cases, 1):
        lines.append("%03d\t%s" % (index, case["generator_args"]))
    return "\n".join(lines) + "\n"


def _cases_json(cases):
    data = [
        {
            "name": "%03d" % index,
            "order": case["order"],
            "generator_args": case["generator_args"],
            "argv": case["generator_args"].split(),
            "input_file": "tests/%03d.in" % index,
            "output_file": "tests/%03d.out" % index,
        }
        for index, case in enumerate(cases, 1)
    ]
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _python_runner(generator_name):
    return """#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import zipfile

GENERATOR_SOURCE = {generator_name!r}
GENERATOR_EXE = "generator.exe" if os.name == "nt" else "generator"
TEST_DIR = "tests"
ZIP_NAME = "static_tests.zip"


def run(cmd, **kwargs):
    print("+", " ".join(cmd))
    return subprocess.run(cmd, check=True, **kwargs)


def compile_generator():
    cxx = os.environ.get("CXX", "g++")
    run([cxx, "-std=c++17", "-O2", GENERATOR_SOURCE, "-o", GENERATOR_EXE])


def materialize():
    with open("cases.json", "r", encoding="utf-8") as f:
        cases = json.load(f)

    os.makedirs(TEST_DIR, exist_ok=True)
    generator_command = os.path.join(".", GENERATOR_EXE)
    for case in cases:
        input_path = case["input_file"]
        output_path = case["output_file"]
        print("generating", case["name"], ":", case["generator_args"])
        with open(input_path, "wb") as input_file, open(output_path, "wb") as output_file:
            run([generator_command, *case["argv"]], stdout=input_file, stderr=output_file)

    if os.path.exists(ZIP_NAME):
        os.remove(ZIP_NAME)
    with zipfile.ZipFile(ZIP_NAME, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(TEST_DIR):
            for filename in sorted(files):
                path = os.path.join(root, filename)
                zf.write(path, path.replace(os.sep, "/"))
    print("wrote", ZIP_NAME)


def main():
    if not shutil.which(os.environ.get("CXX", "g++")):
        sys.exit("g++ not found. Install a C++ compiler or set CXX=/path/to/compiler.")
    compile_generator()
    materialize()


if __name__ == "__main__":
    main()
""".format(generator_name=generator_name)


def _shell_runner(generator_name):
    return """#!/usr/bin/env bash
set -euo pipefail

GENERATOR_SOURCE={quoted_generator_name}
GENERATOR_EXE=generator
TEST_DIR=tests
ZIP_NAME=static_tests.zip
CXX="${{CXX:-g++}}"

command -v "$CXX" >/dev/null 2>&1 || {{
  echo "g++ not found. Install a C++ compiler or set CXX=/path/to/compiler." >&2
  exit 1
}}

"$CXX" -std=c++17 -O2 "$GENERATOR_SOURCE" -o "$GENERATOR_EXE"
mkdir -p "$TEST_DIR"

while IFS=$'\\t' read -r name args; do
  case "$name" in
    ""|"#"*) continue ;;
  esac
  echo "generating $name: $args"
  ./"$GENERATOR_EXE" $args > "$TEST_DIR/$name.in" 2> "$TEST_DIR/$name.out"
done < cases.tsv

rm -f "$ZIP_NAME"
if command -v zip >/dev/null 2>&1; then
  zip -qr "$ZIP_NAME" "$TEST_DIR"
else
  echo "zip command not found. The tests were generated in $TEST_DIR." >&2
  echo "Install zip or compress the $TEST_DIR folder manually." >&2
  exit 1
fi

echo "wrote $ZIP_NAME"
""".format(quoted_generator_name=shlex.quote(generator_name))


def _batch_runner(generator_name):
    return r"""@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "GENERATOR_SOURCE=%s"
set "GENERATOR_EXE=generator.exe"
set "TEST_DIR=tests"
set "ZIP_NAME=static_tests.zip"
if "%%CXX%%"=="" set "CXX=g++"

where "%%CXX%%" >nul 2>nul
if errorlevel 1 (
  echo g++ not found. Install MinGW-w64 or set CXX=C:\path\to\g++.exe.
  exit /b 1
)

"%%CXX%%" -std=c++17 -O2 "%%GENERATOR_SOURCE%%" -o "%%GENERATOR_EXE%%"
if not exist "%%TEST_DIR%%" mkdir "%%TEST_DIR%%"

for /f "usebackq tokens=1,* delims=	" %%%%A in ("cases.tsv") do (
  set "NAME=%%%%A"
  set "ARGS=%%%%B"
  if not "!NAME!"=="" if not "!NAME:~0,1!"=="#" (
    echo generating !NAME!: !ARGS!
    "%%GENERATOR_EXE%%" !ARGS! > "%%TEST_DIR%%\!NAME!.in" 2> "%%TEST_DIR%%\!NAME!.out"
  )
)

if exist "%%ZIP_NAME%%" del "%%ZIP_NAME%%"
powershell -NoProfile -Command "Compress-Archive -Path '%%TEST_DIR%%' -DestinationPath '%%ZIP_NAME%%' -Force"
if errorlevel 1 (
  echo Failed to create %%ZIP_NAME%%. The tests were generated in %%TEST_DIR%%.
  exit /b 1
)

echo wrote %%ZIP_NAME%%
""" % generator_name
