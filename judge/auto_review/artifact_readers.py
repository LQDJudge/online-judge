"""Bounded data summaries; never deserialize executable dataset objects."""

import ast
import base64
import csv
import io
import os
import re
import stat
import struct
import zipfile
from urllib.parse import unquote, urlsplit

from django.core.files.storage import default_storage
from django.urls import Resolver404, resolve

SAFE_ZIP_COMPRESSION = frozenset((zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED))
MAX_OUTPUT_EXAMPLE_BYTES = 20 * 1024 * 1024


class UnsupportedArchiveCompression(ValueError):
    """A valid ZIP may need an unbounded host decompressor; do not open it."""


def output_source_filename(source):
    if not isinstance(source, str) or len(source) > 4096:
        raise ValueError("Invalid output artifact reference")
    try:
        match = resolve(unquote(urlsplit(source.strip()).path))
    except (Resolver404, ValueError) as error:
        raise ValueError(
            "Output artifact must reference a registered site upload"
        ) from error
    filename = match.kwargs.get("filename", "")
    if (
        match.url_name != "submission_source_file"
        or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,199}", filename)
        or ".." in filename
    ):
        raise ValueError("Unsafe output artifact storage reference")
    return filename


def open_output_source(source):
    return default_storage.open("submissions/" + output_source_filename(source), "rb")


def safe_zip_members(archive):
    members = archive.infolist()
    if (
        len(members) > 2000
        or sum(item.file_size for item in members) > 128 * 1024 * 1024
    ):
        raise ValueError("Archive exceeds review extraction limits")
    seen = set()
    for item in members:
        name = item.filename
        mode = item.external_attr >> 16
        if (
            name in seen
            or not name
            or len(name) > 255
            or "\\" in name
            or name.startswith("/")
            or any(part in ("..", ".", "") for part in name.rstrip("/").split("/"))
            or item.flag_bits & 1
            or stat.S_ISLNK(mode)
            or item.file_size > 32 * 1024 * 1024
        ):
            raise ValueError("Unsafe or ambiguous archive member")
        if item.compress_type not in SAFE_ZIP_COMPRESSION:
            raise UnsupportedArchiveCompression(
                "ZIP compression needs manual inspection"
            )
        seen.add(name)
    return members


def summarize_data(raw, filename):
    """Explicitly sampled, schema-focused evidence, never full coverage proof."""
    suffix = os.path.splitext(filename)[1].lower()
    result = {"size": len(raw), "format": suffix.lstrip("."), "complete_scan": False}
    if suffix == ".csv":
        sample = raw[: 256 * 1024]
        # Drop the truncated final line instead of inventing a malformed row.
        if len(sample) < len(raw):
            sample = sample.rsplit(b"\n", 1)[0]
        try:
            rows = list(csv.reader(io.StringIO(sample.decode("utf-8-sig"))))
        except csv.Error as error:
            raise ValueError("CSV exceeds safe field parsing limits") from error
        widths = [len(row) for row in rows]
        result.update(
            header=[cell[:240] for cell in rows[0][:128]] if rows else [],
            sampled_rows=len(rows),
            column_counts=sorted(set(widths)),
            preview=[[cell[:240] for cell in row[:128]] for row in rows[:6]],
            preview_truncated=any(
                len(row) > 128 or any(len(cell) > 240 for cell in row)
                for row in rows[:6]
            ),
            complete_scan=len(sample) == len(raw),
            limitation="CSV rows/header interpretation and roles require contract analysis; a prefix is not whole-dataset coverage.",
        )
    elif suffix == ".npy":
        if raw[:6] != b"\x93NUMPY" or len(raw) < 10:
            raise ValueError("Invalid NPY header")
        version = raw[6]
        size_bytes = 2 if version == 1 else 4 if version in (2, 3) else 0
        if not size_bytes:
            raise ValueError("Unsupported NPY version")
        length = int.from_bytes(raw[8 : 8 + size_bytes], "little")
        if not 0 < length <= 65536 or 8 + size_bytes + length > len(raw):
            raise ValueError("Invalid NPY metadata length")
        header = ast.literal_eval(
            raw[8 + size_bytes : 8 + size_bytes + length].decode("latin1")
        )
        if (
            not isinstance(header, dict)
            or not isinstance(header.get("shape"), tuple)
            or not isinstance(header.get("descr"), str)
        ):
            raise ValueError("Unsupported NPY schema")
        shape = header["shape"]
        if len(shape) > 16 or any(
            not isinstance(x, int) or isinstance(x, bool) or x < 0 for x in shape
        ):
            raise ValueError("Invalid NPY shape")
        dtype = header["descr"]
        if not re.fullmatch(r"[<>=|]?[biufcSU?][0-9]{0,5}", dtype):
            raise ValueError(
                "Object/structured array dtypes require a registered safe reader"
            )
        result.update(
            shape=list(shape),
            dtype=dtype,
            fortran_order=header.get("fortran_order"),
            limitation="Header only: values, nonfinite values and label semantics were not scanned. No pickle or array object loading.",
        )
    elif suffix in (".zip", ".npz"):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = safe_zip_members(archive)
            result["members"] = [
                {"filename": item.filename, "size": item.file_size}
                for item in members[:80]
            ]
            result["member_count"] = len(members)
            result["omitted_members"] = max(0, len(members) - 80)
            if suffix == ".zip":
                for item, record in zip(members[:80], result["members"]):
                    if item.is_dir():
                        continue
                    with archive.open(item) as stream:
                        preview = stream.read(4097)
                    record.update(
                        preview=preview[:512].decode("utf8", "replace"),
                        complete_content=len(preview) <= 4096,
                    )
                    if len(preview) <= 4096:
                        record["content_b64"] = base64.b64encode(preview).decode()
            if suffix == ".npz":
                if any(
                    not item.is_dir() and not item.filename.endswith(".npy")
                    for item in members
                ):
                    raise ValueError("Unexpected NPZ member")
                arrays = []
                for item in members[:80]:
                    if item.is_dir():
                        continue
                    if not item.filename.endswith(".npy"):
                        raise ValueError("Unexpected NPZ member")
                    with archive.open(item) as stream:
                        arrays.append(
                            {
                                "filename": item.filename,
                                **summarize_data(stream.read(65548), item.filename),
                            }
                        )
                result["arrays"] = arrays
            result["limitation"] = (
                "Archive inventory/schema checks are bounded and do not prove data semantics or score coverage."
            )
    elif suffix == ".png" and len(raw) >= 24 and raw[:8] == b"\x89PNG\r\n\x1a\n":
        result.update(
            width=struct.unpack(">I", raw[16:20])[0],
            height=struct.unpack(">I", raw[20:24])[0],
            limitation="Image header only; pixels and semantic labels were not reviewed.",
        )
    else:
        result.update(
            available=False,
            limitation="No registered schema reader for this format; do not infer data correctness from its filename.",
        )
    return result
