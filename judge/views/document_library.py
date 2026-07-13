import os

from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import Http404, FileResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from judge.caching import cache_wrapper
from judge.utils.storage_helpers import (
    storage_delete_file,
    storage_file_exists,
    storage_listdir,
    storage_rename_file,
    validate_path_prefix,
)

LIBRARY_ROOT = "library"


@cache_wrapper(prefix="lib_dir", timeout=300)
def _cached_listdir(rel):
    """Cached (dirs, files) for one library folder level, keyed by relative path.

    Read-only path only. Write operations must NOT use this (they need fresh
    data) and must call `_dirty_listing(rel)` for the folders they change.
    """
    prefix = LIBRARY_ROOT if not rel else f"{LIBRARY_ROOT}/{rel}"
    return storage_listdir(default_storage, prefix)


def _parent_path(rel):
    """Return the parent folder of a library-relative path ('' for root-level)."""
    rel = (rel or "").strip("/")
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _dirty_listing(*rels):
    """Invalidate the cached listing for the given folder path(s)."""
    for rel in rels:
        _cached_listdir.dirty((rel or "").strip("/"))


def get_validated_library_pdf(path):
    """Resolve a library-relative path to a storage key for an existing PDF.

    Raises Http404 on traversal, non-PDF, or missing file.
    """
    file_path = f"{LIBRARY_ROOT}/{path}"
    if not validate_path_prefix(file_path, LIBRARY_ROOT):
        raise Http404("Not found")
    if not path.lower().endswith(".pdf"):
        raise Http404("Not a PDF")
    if not storage_file_exists(default_storage, file_path):
        raise Http404("Not found")
    return file_path


def library_raw(request, path):
    """Stream a library PDF inline, same-origin (feeds the viewer)."""
    file_path = get_validated_library_pdf(path)
    response = FileResponse(
        default_storage.open(file_path, "rb"), content_type="application/pdf"
    )
    response["Content-Disposition"] = "inline"
    return response


def library_document(request, path):
    """Reader page for a library PDF (hybrid viewer)."""
    get_validated_library_pdf(path)  # 404s on bad/missing/non-pdf
    context = {
        "title": os.path.basename(path),
        "raw_url": reverse("library_raw", args=[path]),
        "layout": "no_wrapper",  # full-bleed reader (no wrapper padding/max-width)
    }
    return render(request, "reader/read.html", context)


def get_library_listing(path=""):
    """List one folder level of the library.

    Returns (exists, breadcrumbs, folders, files):
      exists      -- bool; False for a non-existent (empty) non-root folder
      breadcrumbs -- [{"name", "path"}] from root to here
      folders     -- [{"name", "path"}] sorted
      files       -- [{"name", "path", "is_pdf"}] sorted, ".keep" hidden
    """
    rel = (path or "").strip("/")
    dirs, raw_files = _cached_listdir(rel)

    # A non-root folder "exists" if it has any child (dir, file, or .keep marker).
    exists = bool(not rel or dirs or raw_files)

    folders = [{"name": d, "path": f"{rel}/{d}".strip("/")} for d in sorted(dirs)]
    files = [
        {
            "name": f,
            "path": f"{rel}/{f}".strip("/"),
            "is_pdf": f.lower().endswith(".pdf"),
        }
        for f in sorted(raw_files)
        if f != ".keep"
    ]

    breadcrumbs = [{"name": _("Library"), "path": ""}]
    acc = []
    for seg in rel.split("/") if rel else []:
        acc.append(seg)
        breadcrumbs.append({"name": seg, "path": "/".join(acc)})

    return exists, breadcrumbs, folders, files


def library_browse(request, path=""):
    """Public browse of a library folder."""
    rel = (path or "").strip("/")
    full = LIBRARY_ROOT if not rel else f"{LIBRARY_ROOT}/{rel}"
    if not validate_path_prefix(full, LIBRARY_ROOT):
        raise Http404("Not found")

    exists, breadcrumbs, folders, files = get_library_listing(rel)
    if rel and not exists:
        raise Http404("Folder not found")

    return render(
        request,
        "library/browse.html",
        {
            "title": breadcrumbs[-1]["name"],
            "breadcrumbs": breadcrumbs,
            "folders": folders,
            "files": files,
            "current_path": rel,
            "can_edit": request.user.is_authenticated and request.user.is_superuser,
        },
    )


def library_catalog(request):
    """Public Codeforces-style catalog: an indented, lazy-expand tree.

    Only the root level is server-rendered; deeper folders load on expand via
    library_api_list.
    """
    _exists, _crumbs, folders, files = get_library_listing("")
    return render(
        request,
        "library/catalog.html",
        {"title": _("Library"), "folders": folders, "files": files},
    )


def library_api_list(request):
    """Public JSON: one folder level's children (for lazy tree expansion)."""
    rel = (request.GET.get("path") or "").strip("/")
    full = LIBRARY_ROOT if not rel else f"{LIBRARY_ROOT}/{rel}"
    if not validate_path_prefix(full, LIBRARY_ROOT):
        return JsonResponse({"error": _("Invalid path")}, status=400)
    _exists, _crumbs, folders, files = get_library_listing(rel)
    return JsonResponse({"folders": folders, "files": files})


def library_download(request, path):
    """Public download: redirect to the file's storage URL (CDN in prod)."""
    file_path = f"{LIBRARY_ROOT}/{path}"
    if not validate_path_prefix(file_path, LIBRARY_ROOT):
        raise Http404("Not found")
    if not storage_file_exists(default_storage, file_path):
        raise Http404("Not found")
    return HttpResponseRedirect(default_storage.url(file_path))


# ---------------------------------------------------------------------------
# Superuser management (Drive-style). Structure ops map to storage_helpers.
# ---------------------------------------------------------------------------


def _full(rel):
    """Library-relative path -> full storage key prefix."""
    rel = (rel or "").strip("/")
    return LIBRARY_ROOT if not rel else f"{LIBRARY_ROOT}/{rel}"


def _sanitize_name(name):
    """Sanitize a single path segment; drop slashes/traversal. '' if invalid."""
    name = (name or "").strip().replace("\\", "/")
    name = name.split("/")[-1]
    if name in ("", ".", ".."):
        return ""
    return name


def _iter_library_files(prefix):
    """Yield every object key under a prefix, recursively."""
    dirs, files = storage_listdir(default_storage, prefix)
    for f in files:
        yield f"{prefix}/{f}"
    for d in dirs:
        yield from _iter_library_files(f"{prefix}/{d}")


def _manage_gate_api(request):
    """AJAX gate: 403 JSON for anonymous or non-superuser (no redirect)."""
    if not request.user.is_authenticated or not request.user.is_superuser:
        return JsonResponse({"error": _("Permission denied")}, status=403)
    return None


def _relocate_folder(old_full, new_full):
    """Move/rename a folder by copy+delete of every object under it."""
    if new_full == old_full or new_full.startswith(old_full + "/"):
        return JsonResponse(
            {"error": _("Cannot move a folder into itself")}, status=400
        )
    keys = list(_iter_library_files(old_full))
    if not keys:
        return JsonResponse({"error": _("Folder not found")}, status=404)
    failed = []
    for key in keys:
        suffix = key[len(old_full) :].lstrip("/")
        if not storage_rename_file(default_storage, key, f"{new_full}/{suffix}"):
            failed.append(suffix)
    if failed:
        return JsonResponse(
            {
                "error": _("Some files could not be moved: %(files)s")
                % {"files": ", ".join(failed)}
            },
            status=500,
        )
    return JsonResponse({"success": True})


@login_required
@require_POST
def library_manage_create_folder(request):
    gate = _manage_gate_api(request)
    if gate:
        return gate
    parent_full = _full(request.POST.get("path"))
    name = _sanitize_name(request.POST.get("name"))
    if not validate_path_prefix(parent_full, LIBRARY_ROOT):
        return JsonResponse({"error": _("Invalid path")}, status=400)
    if not name:
        return JsonResponse({"error": _("Invalid folder name")}, status=400)
    key = f"{parent_full}/{name}/.keep"
    if storage_file_exists(default_storage, key):
        return JsonResponse({"error": _("Folder already exists")}, status=400)
    default_storage.save(key, ContentFile(b""))
    _dirty_listing((request.POST.get("path") or "").strip("/"))
    return JsonResponse({"success": True})


@login_required
@require_POST
def library_manage_upload(request):
    gate = _manage_gate_api(request)
    if gate:
        return gate
    parent_full = _full(request.POST.get("path"))
    if not validate_path_prefix(parent_full, LIBRARY_ROOT):
        return JsonResponse({"error": _("Invalid path")}, status=400)
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"error": _("No file provided")}, status=400)
    name = _sanitize_name(f.name)
    if not name.lower().endswith(".pdf"):
        return JsonResponse({"error": _("Only PDF files are allowed")}, status=400)
    key = f"{parent_full}/{name}"
    if storage_file_exists(default_storage, key):
        return JsonResponse(
            {"error": _("A file with this name already exists")}, status=400
        )
    default_storage.save(key, f)
    _dirty_listing((request.POST.get("path") or "").strip("/"))
    return JsonResponse({"success": True})


@login_required
@require_POST
def library_manage_rename(request):
    gate = _manage_gate_api(request)
    if gate:
        return gate
    rel = (request.POST.get("path") or "").strip("/")
    new_name = _sanitize_name(request.POST.get("name"))
    kind = request.POST.get("kind")
    full = _full(rel)
    if not rel or not validate_path_prefix(full, LIBRARY_ROOT):
        return JsonResponse({"error": _("Invalid path")}, status=400)
    if not new_name:
        return JsonResponse({"error": _("Invalid name")}, status=400)
    parent = "/".join(full.split("/")[:-1])
    new_full = f"{parent}/{new_name}"
    if kind == "folder":
        resp = _relocate_folder(full, new_full)
        # dirty the folder's own listing too (old path is now stale)
        _dirty_listing(_parent_path(rel), rel)
        return resp
    if not new_full.lower().endswith(".pdf"):
        new_full += ".pdf"
    if storage_file_exists(default_storage, new_full):
        return JsonResponse({"error": _("Target already exists")}, status=400)
    if not storage_rename_file(default_storage, full, new_full):
        return JsonResponse({"error": _("Rename failed")}, status=500)
    _dirty_listing(_parent_path(rel))
    return JsonResponse({"success": True})


@login_required
@require_POST
def library_manage_move(request):
    gate = _manage_gate_api(request)
    if gate:
        return gate
    rel = (request.POST.get("path") or "").strip("/")
    dest = (request.POST.get("dest") or "").strip("/")
    kind = request.POST.get("kind")
    full = _full(rel)
    dest_full = _full(dest)
    if (
        not rel
        or not validate_path_prefix(full, LIBRARY_ROOT)
        or not validate_path_prefix(dest_full, LIBRARY_ROOT)
    ):
        return JsonResponse({"error": _("Invalid path")}, status=400)
    base = full.split("/")[-1]
    new_full = f"{dest_full}/{base}"
    if kind == "folder":
        resp = _relocate_folder(full, new_full)
        _dirty_listing(_parent_path(rel), rel, dest)
        return resp
    if storage_file_exists(default_storage, new_full):
        return JsonResponse({"error": _("Target already exists")}, status=400)
    if not storage_rename_file(default_storage, full, new_full):
        return JsonResponse({"error": _("Move failed")}, status=500)
    _dirty_listing(_parent_path(rel), dest)
    return JsonResponse({"success": True})


@login_required
@require_POST
def library_manage_delete(request):
    gate = _manage_gate_api(request)
    if gate:
        return gate
    rel = (request.POST.get("path") or "").strip("/")
    kind = request.POST.get("kind")
    full = _full(rel)
    if not rel or not validate_path_prefix(full, LIBRARY_ROOT):
        return JsonResponse({"error": _("Invalid path")}, status=400)
    if kind == "folder":
        dirs, files = storage_listdir(default_storage, full)
        non_keep = [f for f in files if f != ".keep"]
        if dirs or non_keep:
            return JsonResponse({"error": _("Folder is not empty")}, status=400)
        for f in files:  # remove the .keep marker(s)
            storage_delete_file(default_storage, f"{full}/{f}")
        _dirty_listing(_parent_path(rel), rel)
        return JsonResponse({"success": True})
    if not storage_file_exists(default_storage, full):
        return JsonResponse({"error": _("File not found")}, status=404)
    storage_delete_file(default_storage, full)
    _dirty_listing(_parent_path(rel))
    return JsonResponse({"success": True})
