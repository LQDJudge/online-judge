# Kaggle-like Problems Implementation Plan (Site repo)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make LQDOJ a viable host for Kaggle-style output-only problems by (a) adding general per-problem file attachments (Codeforces-style materials), (b) adding new CSV-aware scoring checkers, and (c) upgrading the submit page to use presigned direct-upload for large output files.

**Architecture:** Three near-independent tracks layered on the existing output-only pipeline. Attachments are a new model + tab. CSV checker keys are added to `CHECKERS` (implementation in judge-server fork — separate plan, out of scope here). Submit-page upgrade reuses `judge/views/direct_upload.py`. No changes to `ProblemData` schema; no new judge-bridge protocol; no new submission storage.

**Tech Stack:** Django 4.x, Jinja2 templates, jQuery (existing), AWS S3 / presigned uploads, `django-ratelimit`, `pytest`-style Django tests under `judge/tests/`.

**In scope (this plan):**
- Site repo work (Tasks 1–9).
- Judge-server `dmoj/checkers/csv_*.py` implementations at `~/LQDOJ/judge-server/dmoj/checkers/` (Task 10).

**In scope (extension):**
- Public/Private leaderboard split via a `pretest_fraction` arg on `csv_*` checkers (Task 11). Author sets `0.5` during contest; after contest, sets `1.0` and rejudges all submissions to reveal final scores.

**Out of scope:** On-platform training infrastructure, notebooks, GPU.

---

## File Structure

**New files (site repo):**

| File | Purpose |
|---|---|
| `judge/models/problem_attachment.py` | `ProblemAttachment` model (FK→Problem, file, description, order) |
| `judge/migrations/0236_problem_attachment.py` | Auto-generated migration adding the table |
| `judge/forms.py` (modify) | `ProblemAttachmentForm` for upload validation |
| `judge/views/problem_attachment.py` | Tab view + AJAX upload/reorder/delete + download view |
| `templates/problem/attachments.html` | "Attachments" tab edit page |
| `templates/problem/attachment_section.html` | Partial: file list rendered into `problem.html` |
| `judge/tests/test_problem_attachment.py` | Unit tests for model + views |
| `dmoj/urls.py` (modify) | URL wiring under `/problem/<code>/attachments/...` |
| `judge/jinja2/__init__.py` (modify) | Register filter `filesizeformat` if not present (it is — verify only) |

**Modified files:**

| File | Change |
|---|---|
| `judge/models/__init__.py` | Import `ProblemAttachment` |
| `judge/models/problem_data.py:34-48` | Add 6 `csv_*` entries to `CHECKERS` |
| `judge/views/problem_data.py:73` | Extend `checker_args_cleaner` to accept CSV args |
| `templates/problem/data.html` (or wherever the checker_args UI lives — verify) | Render CSV arg inputs when `csv_*` selected |
| `templates/problem/problem.html:448` | Include `attachment_section.html` partial |
| `templates/problem/submit.html:5-41,184-199` | Drag-drop UX + presigned-upload path |
| `judge/forms.py:198` | Bump global submission size cap to 50 MB |
| `judge/views/direct_upload.py:29` | Add `@ratelimit` decorator on `get_upload_config` |

---

## Task 1: ProblemAttachment model

**Files:**
- Create: `judge/models/problem_attachment.py`
- Modify: `judge/models/__init__.py`
- Test: `judge/tests/test_problem_attachment.py`

- [ ] **Step 1.1: Write the failing model test**

Create `judge/tests/test_problem_attachment.py`:

```python
from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from judge.models import Problem, ProblemAttachment, Profile
from django.contrib.auth.models import User


class ProblemAttachmentModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('author', 'a@a.com', 'x')
        cls.profile, _ = Profile.objects.get_or_create(user=cls.user)
        cls.problem = Problem.objects.create(
            code='kaggle1', name='Kaggle 1', description='', time_limit=1, memory_limit=65536,
            points=100, partial=True, group_id=1,
        )

    def test_attachment_default_ordering(self):
        a1 = ProblemAttachment.objects.create(
            problem=self.problem, order=1,
            file=SimpleUploadedFile('a.txt', b'aaa'), description='first',
        )
        a2 = ProblemAttachment.objects.create(
            problem=self.problem, order=0,
            file=SimpleUploadedFile('b.txt', b'bbb'), description='second',
        )
        ordered = list(ProblemAttachment.objects.filter(problem=self.problem))
        self.assertEqual(ordered, [a2, a1])

    def test_cascade_delete_on_problem(self):
        ProblemAttachment.objects.create(
            problem=self.problem, file=SimpleUploadedFile('c.txt', b'ccc'),
        )
        self.problem.delete()
        self.assertEqual(ProblemAttachment.objects.count(), 0)
```

- [ ] **Step 1.2: Run test to verify it fails**

```bash
source ../dmojsite/bin/activate
python3 manage.py test judge.tests.test_problem_attachment -v 2
```
Expected: ImportError on `ProblemAttachment`.

- [ ] **Step 1.3: Create the model**

Create `judge/models/problem_attachment.py`:

```python
import os
from django.db import models
from django.utils.translation import gettext_lazy as _

from judge.models.problem_data import problem_data_storage

__all__ = ['ProblemAttachment']


def problem_attachment_path(instance, filename):
    return os.path.join('problem_attachments', instance.problem.code, os.path.basename(filename))


class ProblemAttachment(models.Model):
    problem = models.ForeignKey(
        'Problem',
        verbose_name=_('problem'),
        related_name='attachments',
        on_delete=models.CASCADE,
    )
    file = models.FileField(
        verbose_name=_('file'),
        upload_to=problem_attachment_path,
        storage=problem_data_storage,
    )
    description = models.CharField(
        verbose_name=_('description'),
        max_length=255,
        blank=True,
    )
    order = models.PositiveIntegerField(
        verbose_name=_('display order'),
        default=0,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('problem attachment')
        verbose_name_plural = _('problem attachments')
        ordering = ['order', 'id']

    def __str__(self):
        return f'{self.problem.code}: {os.path.basename(self.file.name)}'

    @property
    def filename(self):
        return os.path.basename(self.file.name)
```

- [ ] **Step 1.4: Register the model**

Modify `judge/models/__init__.py` — add to existing import block (alphabetical with other model imports):

```python
from judge.models.problem_attachment import ProblemAttachment
```

- [ ] **Step 1.5: Generate the migration**

```bash
python3 manage.py makemigrations judge
```
Expected output: `Migrations for 'judge': judge/migrations/0236_problemattachment.py - Create model ProblemAttachment`.

- [ ] **Step 1.6: Apply the migration**

```bash
python3 manage.py migrate
```
Expected: `Applying judge.0236_problemattachment... OK`.

- [ ] **Step 1.7: Run the test**

```bash
python3 manage.py test judge.tests.test_problem_attachment -v 2
```
Expected: 2 tests pass.

- [ ] **Step 1.8: Commit**

```bash
git add judge/models/problem_attachment.py judge/models/__init__.py \
        judge/migrations/0236_problemattachment.py judge/tests/test_problem_attachment.py
git commit -m "Add ProblemAttachment model"
```

---

## Task 2: Attachment form (validation + size cap)

**Files:**
- Modify: `judge/forms.py`
- Test: `judge/tests/test_problem_attachment.py`

- [ ] **Step 2.1: Add the failing form test**

Append to `judge/tests/test_problem_attachment.py`:

```python
from judge.forms import ProblemAttachmentForm


class ProblemAttachmentFormTests(TestCase):
    def test_rejects_oversize_file(self):
        big = SimpleUploadedFile('big.bin', b'\x00' * (101 * 1024 * 1024))
        form = ProblemAttachmentForm(
            data={'description': 'x', 'order': 0},
            files={'file': big},
        )
        self.assertFalse(form.is_valid())
        self.assertIn('file', form.errors)

    def test_accepts_normal_file(self):
        small = SimpleUploadedFile('train.csv', b'id,x\n1,2\n')
        form = ProblemAttachmentForm(
            data={'description': 'training data', 'order': 0},
            files={'file': small},
        )
        self.assertTrue(form.is_valid(), form.errors)
```

- [ ] **Step 2.2: Run, expect failure**

```bash
python3 manage.py test judge.tests.test_problem_attachment.ProblemAttachmentFormTests -v 2
```
Expected: ImportError on `ProblemAttachmentForm`.

- [ ] **Step 2.3: Add the form**

Append to `judge/forms.py` (after the existing `file_size_validator` at line 197):

```python
PROBLEM_ATTACHMENT_MAX_SIZE = 100 * 1024 * 1024  # 100 MB


def attachment_size_validator(file):
    if file.size > PROBLEM_ATTACHMENT_MAX_SIZE:
        raise ValidationError(
            _('File too large. Size should not exceed %(limit)d MB.') % {
                'limit': PROBLEM_ATTACHMENT_MAX_SIZE // (1024 * 1024),
            }
        )


class ProblemAttachmentForm(ModelForm):
    file = FileField(required=True, validators=[attachment_size_validator])

    class Meta:
        model = ProblemAttachment
        fields = ['file', 'description', 'order']
```

Add to imports at top of file if not already present:

```python
from judge.models import ProblemAttachment
```

- [ ] **Step 2.4: Run tests, expect pass**

```bash
python3 manage.py test judge.tests.test_problem_attachment -v 2
```
Expected: 4 tests pass.

- [ ] **Step 2.5: Commit**

```bash
git add judge/forms.py judge/tests/test_problem_attachment.py
git commit -m "Add ProblemAttachmentForm with 100MB limit"
```

---

## Task 3: Attachment management view (tab + AJAX endpoints)

**Files:**
- Create: `judge/views/problem_attachment.py`
- Test: `judge/tests/test_problem_attachment.py`

- [ ] **Step 3.1: Write failing view tests**

Append to `judge/tests/test_problem_attachment.py`:

```python
from django.urls import reverse
from django.test import Client


class ProblemAttachmentViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.author = User.objects.create_user('author', 'a@a.com', 'pw')
        cls.author_profile, _ = Profile.objects.get_or_create(user=cls.author)
        cls.outsider = User.objects.create_user('outsider', 'o@o.com', 'pw')
        Profile.objects.get_or_create(user=cls.outsider)
        cls.problem = Problem.objects.create(
            code='kp', name='KP', description='', time_limit=1, memory_limit=65536,
            points=100, partial=True, group_id=1,
        )
        cls.problem.authors.add(cls.author_profile)

    def setUp(self):
        self.client = Client()

    def _login(self, user):
        self.client.force_login(user)

    def test_tab_requires_edit_permission(self):
        url = reverse('problem_attachments', args=['kp'])
        self._login(self.outsider)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_tab_renders_for_author(self):
        self._login(self.author)
        resp = self.client.get(reverse('problem_attachments', args=['kp']))
        self.assertEqual(resp.status_code, 200)

    def test_upload_creates_attachment(self):
        self._login(self.author)
        resp = self.client.post(
            reverse('problem_attachment_upload', args=['kp']),
            {
                'file': SimpleUploadedFile('train.csv', b'id,x\n1,2\n'),
                'description': 'training data',
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body['success'])
        self.assertEqual(ProblemAttachment.objects.filter(problem=self.problem).count(), 1)

    def test_delete_removes_attachment(self):
        att = ProblemAttachment.objects.create(
            problem=self.problem, file=SimpleUploadedFile('x.csv', b'x'),
        )
        self._login(self.author)
        resp = self.client.post(
            reverse('problem_attachment_delete', args=['kp', att.id]),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(ProblemAttachment.objects.filter(id=att.id).exists())

    def test_reorder_updates_order(self):
        a1 = ProblemAttachment.objects.create(problem=self.problem, order=0, file=SimpleUploadedFile('1', b'1'))
        a2 = ProblemAttachment.objects.create(problem=self.problem, order=1, file=SimpleUploadedFile('2', b'2'))
        self._login(self.author)
        resp = self.client.post(
            reverse('problem_attachment_reorder', args=['kp']),
            data={'order': [str(a2.id), str(a1.id)]},
        )
        self.assertEqual(resp.status_code, 200)
        a1.refresh_from_db(); a2.refresh_from_db()
        self.assertEqual(a2.order, 0)
        self.assertEqual(a1.order, 1)
```

- [ ] **Step 3.2: Run, expect failure**

```bash
python3 manage.py test judge.tests.test_problem_attachment.ProblemAttachmentViewTests -v 2
```
Expected: NoReverseMatch errors (URLs don't exist yet).

- [ ] **Step 3.3: Create the view module**

Create `judge/views/problem_attachment.py`:

```python
import json

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse, HttpResponse, FileResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST, require_GET
from django.views.generic import View

from judge.forms import ProblemAttachmentForm
from judge.models import Problem, ProblemAttachment


def _can_edit_problem(user, problem):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    profile = getattr(user, 'profile', None)
    if profile is None:
        return False
    return (
        problem.is_editable_by(user)
        if hasattr(problem, 'is_editable_by')
        else profile in problem.authors.all() or profile in problem.curators.all()
    )


def _can_view_problem(user, problem):
    return problem.is_accessible_by(user) if hasattr(problem, 'is_accessible_by') else True


@login_required
@ensure_csrf_cookie
def attachments_tab(request, problem):
    problem_obj = get_object_or_404(Problem, code=problem)
    if not _can_edit_problem(request.user, problem_obj):
        raise PermissionDenied
    attachments = problem_obj.attachments.all()
    return render(request, 'problem/attachments.html', {
        'problem': problem_obj,
        'attachments': attachments,
        'title': _('Attachments for %s') % problem_obj.name,
    })


@login_required
@require_POST
def attachment_upload(request, problem):
    problem_obj = get_object_or_404(Problem, code=problem)
    if not _can_edit_problem(request.user, problem_obj):
        return JsonResponse({'success': False, 'error': _('Permission denied')}, status=403)

    form = ProblemAttachmentForm(request.POST, request.FILES)
    if not form.is_valid():
        return JsonResponse({'success': False, 'errors': form.errors}, status=400)

    next_order = (
        problem_obj.attachments.aggregate(m=__import__('django.db.models', fromlist=['Max']).Max('order'))['m'] or 0
    ) + 1
    att = form.save(commit=False)
    att.problem = problem_obj
    if not att.order:
        att.order = next_order
    att.save()
    return JsonResponse({
        'success': True,
        'id': att.id,
        'filename': att.filename,
        'description': att.description,
        'order': att.order,
        'size': att.file.size,
    })


@login_required
@require_POST
def attachment_delete(request, problem, attachment_id):
    problem_obj = get_object_or_404(Problem, code=problem)
    if not _can_edit_problem(request.user, problem_obj):
        return JsonResponse({'success': False, 'error': _('Permission denied')}, status=403)
    att = get_object_or_404(ProblemAttachment, id=attachment_id, problem=problem_obj)
    att.file.delete(save=False)
    att.delete()
    return JsonResponse({'success': True})


@login_required
@require_POST
def attachment_reorder(request, problem):
    problem_obj = get_object_or_404(Problem, code=problem)
    if not _can_edit_problem(request.user, problem_obj):
        return JsonResponse({'success': False, 'error': _('Permission denied')}, status=403)

    ids = request.POST.getlist('order') or request.POST.getlist('order[]')
    if not ids:
        try:
            ids = json.loads(request.body or '{}').get('order', [])
        except json.JSONDecodeError:
            return HttpResponseBadRequest('invalid body')

    for new_order, raw_id in enumerate(ids):
        try:
            att_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        ProblemAttachment.objects.filter(id=att_id, problem=problem_obj).update(order=new_order)
    return JsonResponse({'success': True})


@require_GET
def attachment_download(request, problem, attachment_id):
    problem_obj = get_object_or_404(Problem, code=problem)
    if not _can_view_problem(request.user, problem_obj):
        raise PermissionDenied
    att = get_object_or_404(ProblemAttachment, id=attachment_id, problem=problem_obj)

    storage = att.file.storage
    if hasattr(storage, 'url') and getattr(storage, 'custom_domain', None) is None and hasattr(storage, 'connection'):
        url = storage.url(
            att.file.name,
            parameters={'ResponseContentDisposition': f'attachment; filename="{att.filename}"'},
        )
        from django.http import HttpResponseRedirect
        return HttpResponseRedirect(url)

    response = FileResponse(att.file.open('rb'), as_attachment=True, filename=att.filename)
    return response
```

(Re-import note: the inline `__import__('django.db.models', fromlist=['Max'])` is ugly — replace with a top-level `from django.db.models import Max` and call `Max('order')` directly. **Step 3.4 fixes this.**)

- [ ] **Step 3.4: Replace inline `__import__` with proper top-level import**

In `judge/views/problem_attachment.py`:
- Add to imports: `from django.db.models import Max`
- Replace `__import__('django.db.models', fromlist=['Max']).Max('order')` with `Max('order')`.

- [ ] **Step 3.5: Wire up URLs**

Modify `dmoj/urls.py` — find the `^/test_data` block (around line 391) and add inside the same problem URL group:

```python
re_path(
    r'^/attachments$', attachments_tab, name='problem_attachments',
),
re_path(
    r'^/attachments/upload$', attachment_upload, name='problem_attachment_upload',
),
re_path(
    r'^/attachments/reorder$', attachment_reorder, name='problem_attachment_reorder',
),
re_path(
    r'^/attachments/(?P<attachment_id>\d+)/delete$',
    attachment_delete, name='problem_attachment_delete',
),
re_path(
    r'^/attachments/(?P<attachment_id>\d+)$',
    attachment_download, name='problem_attachment_download',
),
```

Add to imports at top of `dmoj/urls.py`:

```python
from judge.views.problem_attachment import (
    attachments_tab, attachment_upload, attachment_delete,
    attachment_reorder, attachment_download,
)
```

- [ ] **Step 3.6: Create minimal template**

Create `templates/problem/attachments.html`:

```jinja
{% extends "common-content.html" %}
{% block title %}{{ title }}{% endblock %}

{% block media %}
<link rel="stylesheet" href="{{ static('problem_attachments.css') }}">
{% endblock %}

{% block js_media %}
<script src="{{ static('problem_attachments.js') }}"></script>
<script>window.PROBLEM_CODE = "{{ problem.code }}";</script>
{% endblock %}

{% block body %}
<div class="problem-attachments-page">
  <h2>{{ _('Attachments for %(name)s') | format(name=problem.name) }}</h2>

  <form id="attachment-upload-form" enctype="multipart/form-data" method="post">
    {% csrf_token %}
    <label>{{ _('File') }}: <input type="file" name="file" required></label>
    <label>{{ _('Description') }}: <input type="text" name="description" maxlength="255"></label>
    <button type="submit">{{ _('Upload') }}</button>
  </form>

  <table id="attachment-list">
    <thead><tr>
      <th>{{ _('Order') }}</th><th>{{ _('Filename') }}</th>
      <th>{{ _('Description') }}</th><th>{{ _('Size') }}</th><th></th>
    </tr></thead>
    <tbody>
    {% for att in attachments %}
      <tr data-id="{{ att.id }}">
        <td class="drag-handle">&#x2630;</td>
        <td><a href="{{ url('problem_attachment_download', problem.code, att.id) }}">{{ att.filename }}</a></td>
        <td>{{ att.description }}</td>
        <td>{{ att.file.size | filesizeformat }}</td>
        <td><button class="delete-btn" data-id="{{ att.id }}">{{ _('Delete') }}</button></td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
</div>
{% endblock %}
```

(JS file `resources/problem_attachments.js` is added in Task 4. The template renders without it — just no drag-drop / no AJAX delete.)

- [ ] **Step 3.7: Run view tests**

```bash
python3 manage.py test judge.tests.test_problem_attachment.ProblemAttachmentViewTests -v 2
```
Expected: all 5 tests pass.

If `is_editable_by` doesn't exist on `Problem`, the `_can_edit_problem` fallback (authors + curators) handles it — no further change needed.

- [ ] **Step 3.8: Commit**

```bash
git add judge/views/problem_attachment.py templates/problem/attachments.html dmoj/urls.py \
        judge/tests/test_problem_attachment.py
git commit -m "Add ProblemAttachment views, AJAX endpoints, and Attachments tab"
```

---

## Task 4: Frontend JS for upload / reorder / delete

**Files:**
- Create: `resources/problem_attachments.js`
- Create: `resources/problem_attachments.scss`

This is wiring, not logic — no unit tests; verified by Playwright in Task 9.

- [ ] **Step 4.1: Create the JS**

Create `resources/problem_attachments.js`:

```javascript
$(function() {
    var problemCode = window.PROBLEM_CODE;
    var csrf = $.cookie ? $.cookie('csrftoken') : null;
    if (!csrf) {
        csrf = document.cookie.split('; ').find(function(r) { return r.startsWith('csrftoken='); });
        csrf = csrf ? csrf.split('=')[1] : '';
    }
    $.ajaxSetup({ headers: { 'X-CSRFToken': csrf } });

    $('#attachment-upload-form').on('submit', function(e) {
        e.preventDefault();
        var fd = new FormData(this);
        $.ajax({
            url: '/problem/' + problemCode + '/attachments/upload',
            method: 'POST', data: fd, processData: false, contentType: false,
            success: function() { location.reload(); },
            error: function(xhr) { alert('Upload failed: ' + (xhr.responseJSON && xhr.responseJSON.error || xhr.statusText)); },
        });
    });

    $('#attachment-list').on('click', '.delete-btn', function() {
        if (!confirm('Delete this attachment?')) return;
        var id = $(this).data('id');
        $.post('/problem/' + problemCode + '/attachments/' + id + '/delete', function() {
            $('tr[data-id="' + id + '"]').remove();
        });
    });

    if (typeof Sortable !== 'undefined') {
        Sortable.create($('#attachment-list tbody')[0], {
            handle: '.drag-handle',
            onEnd: function() {
                var ids = $('#attachment-list tbody tr').map(function() { return $(this).data('id'); }).get();
                $.post('/problem/' + problemCode + '/attachments/reorder', { 'order[]': ids });
            },
        });
    }
});
```

(`Sortable` is SortableJS — already vendored elsewhere on the site for similar drag-drop tables. If not available, the table still works without reorder; add to template `<script src="...">` if needed.)

- [ ] **Step 4.2: Create the SCSS**

Create `resources/problem_attachments.scss`:

```scss
.problem-attachments-page {
    table#attachment-list {
        width: 100%;
        border-collapse: collapse;
        margin-top: 1em;

        th, td {
            padding: 0.5em;
            border-bottom: 1px solid $border_gray;
            text-align: left;
        }

        .drag-handle {
            cursor: grab;
            width: 2em;
            color: $widget_black;
        }

        .delete-btn {
            color: $announcement_red;
            background: transparent;
            border: 1px solid $announcement_red;
            padding: 0.2em 0.5em;
            cursor: pointer;
        }
    }

    #attachment-upload-form {
        background: $background_light_gray;
        padding: 1em;
        border-radius: 4px;
        label { display: inline-block; margin-right: 1em; }
    }
}
```

Add `@import "problem_attachments";` to the relevant entry SCSS file (likely `resources/style.scss` — verify with `grep -n "@import" resources/style.scss | tail -5`).

- [ ] **Step 4.3: Compile CSS**

```bash
./make_style.sh && python3 manage.py collectstatic --noinput
```

- [ ] **Step 4.4: Commit**

```bash
git add resources/problem_attachments.js resources/problem_attachments.scss resources/style.scss
git commit -m "Add frontend JS+SCSS for attachment management"
```

---

## Task 5: Render attachments on the problem page

**Files:**
- Create: `templates/problem/attachment_section.html`
- Modify: `templates/problem/problem.html`

- [ ] **Step 5.1: Create the partial**

Create `templates/problem/attachment_section.html`:

```jinja
{% if problem.attachments.all() %}
<div class="problem-attachments-list">
  <h3>{{ _('Files') }}</h3>
  <ul>
  {% for att in problem.attachments.all() %}
    <li>
      <a href="{{ url('problem_attachment_download', problem.code, att.id) }}">
        {{ att.filename }}
      </a>
      {% if att.description %} — {{ att.description }}{% endif %}
      <span class="muted">({{ att.file.size | filesizeformat }})</span>
    </li>
  {% endfor %}
  </ul>
</div>
{% endif %}
```

- [ ] **Step 5.2: Include the partial in the problem page**

Modify `templates/problem/problem.html` — inside `{% block description %}` near the bottom (around line 444, after the license block, before the block closes) add:

```jinja
  {% include "problem/attachment_section.html" %}
```

- [ ] **Step 5.3: Add minimal styling**

Append to `resources/problem_attachments.scss`:

```scss
.problem-attachments-list {
    margin-top: 1.5em;
    h3 { margin-bottom: 0.5em; }
    ul { list-style: disc; padding-left: 1.5em; }
    .muted { color: $widget_black; opacity: 0.6; font-size: 0.9em; }
}
```

- [ ] **Step 5.4: Recompile and check manually**

```bash
./make_style.sh && python3 manage.py collectstatic --noinput
python3 manage.py runserver 0.0.0.0:8000 &
```

Open `http://localhost:8000/problem/<a problem with attachments>/`. Verify the "Files" section shows the attachment links.

- [ ] **Step 5.5: Commit**

```bash
git add templates/problem/attachment_section.html templates/problem/problem.html \
        resources/problem_attachments.scss
git commit -m "Render attachments on problem page"
```

---

## Task 6: CSV checker keys + checker_args UI

**Files:**
- Modify: `judge/models/problem_data.py:34-48`
- Modify: `judge/views/problem_data.py:73`
- Modify: form template for problem-data edit (verify path — likely `templates/problem/data.html`)
- Test: `judge/tests/test_problem_attachment.py`

> NOTE: The actual checker implementations live in the judge-server fork. This task only adds the choice keys and the form-side `checker_args` validation. A problem configured with `csv_*` will fail to grade until the judge-server release ships those checkers — that's expected.

- [ ] **Step 6.1: Write failing test for checker_args validation**

Append to `judge/tests/test_problem_attachment.py`:

```python
from judge.views.problem_data import checker_args_cleaner
from unittest.mock import MagicMock


class CheckerArgsTests(TestCase):
    def test_csv_args_round_trip(self):
        form = MagicMock()
        form.cleaned_data = {
            'checker': 'csv_rmse',
            'checker_args': '{"has_header": true, "id_column": "id", "label_column": "y"}',
        }
        result = checker_args_cleaner(form)
        import json as _j
        self.assertEqual(_j.loads(result), {
            'has_header': True, 'id_column': 'id', 'label_column': 'y',
        })

    def test_csv_args_rejects_missing_columns(self):
        form = MagicMock()
        form.cleaned_data = {
            'checker': 'csv_rmse',
            'checker_args': '{"has_header": true}',
        }
        with self.assertRaises(__import__('django.core.exceptions', fromlist=['ValidationError']).ValidationError):
            checker_args_cleaner(form)
```

- [ ] **Step 6.2: Run, expect failure**

```bash
python3 manage.py test judge.tests.test_problem_attachment.CheckerArgsTests -v 2
```
Expected: AssertionError on second test (no validation yet).

- [ ] **Step 6.3: Add `csv_*` keys to CHECKERS**

Modify `judge/models/problem_data.py:34-48` — append to the `CHECKERS` tuple:

```python
CHECKERS = (
    ('standard', _('Standard')),
    ('floats', _('Floats')),
    ('floatsabs', _('Floats (absolute)')),
    ('floatsrel', _('Floats (relative)')),
    ('rstripped', _('Non-trailing spaces')),
    ('sorted', _('Unordered')),
    ('identical', _('Byte identical')),
    ('linecount', _('Line-by-line')),
    ('custom', _('Custom checker (PY)')),
    ('customcpp', _('Custom checker (CPP)')),
    ('interact', _('Interactive')),
    ('testlib', _('Testlib')),
    ('interacttl', _('Interactive (Testlib)')),
    ('csv_accuracy', _('CSV: accuracy')),
    ('csv_rmse', _('CSV: RMSE')),
    ('csv_mae', _('CSV: MAE')),
    ('csv_f1', _('CSV: F1 (macro)')),
    ('csv_auc', _('CSV: AUC (binary)')),
    ('csv_logloss', _('CSV: log loss')),
)

CSV_CHECKER_KEYS = {'csv_accuracy', 'csv_rmse', 'csv_mae', 'csv_f1', 'csv_auc', 'csv_logloss'}
```

- [ ] **Step 6.4: Bump CharField max_length for the new keys**

Modify the `checker` field in the same file — change `max_length=10` to `max_length=20`:

```python
checker = models.CharField(
    max_length=20, verbose_name=_("checker"), choices=CHECKERS, blank=True
)
```

Generate migration:

```bash
python3 manage.py makemigrations judge
python3 manage.py migrate
```

Expected: `0237_alter_problemdata_checker.py` created and applied.

- [ ] **Step 6.5: Extend `checker_args_cleaner`**

Modify `judge/views/problem_data.py` — find `checker_args_cleaner` (around line 73). Replace its body with:

```python
def checker_args_cleaner(self):
    from judge.models.problem_data import CSV_CHECKER_KEYS
    data = self.cleaned_data["checker_args"]
    checker = self.cleaned_data.get("checker", "")
    if data in (None, ""):
        if checker in CSV_CHECKER_KEYS:
            raise forms.ValidationError(
                _("CSV checkers require checker_args with id_column and label_column."),
            )
        return data
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        raise forms.ValidationError(_("Invalid JSON in checker arguments."))
    if not isinstance(parsed, dict):
        raise forms.ValidationError(_("Checker arguments must be a JSON object."))
    if checker in CSV_CHECKER_KEYS:
        for required in ("id_column", "label_column"):
            if not parsed.get(required):
                raise forms.ValidationError(
                    _("CSV checker requires '%(field)s' in checker_args.") % {"field": required},
                )
        parsed.setdefault("has_header", True)
    return json.dumps(parsed)
```

Add `import json` and `from django import forms` if not already imported in the file.

- [ ] **Step 6.6: Run tests, expect pass**

```bash
python3 manage.py test judge.tests.test_problem_attachment.CheckerArgsTests -v 2
```
Expected: 2 tests pass.

- [ ] **Step 6.7: Surface CSV args in the problem-data form UI**

Locate the problem-data edit template (run `grep -rn "checker_args" templates/`). Find where `checker` and `checker_args` are rendered. Add a small JS snippet that, when a `csv_*` value is selected, replaces the textarea with three inputs (`id_column`, `label_column`, `has_header`) and serializes them back to JSON on submit.

Append to the template (after the `checker_args` field, inside the data-edit form):

```html
<script>
(function() {
  var csvKeys = ['csv_accuracy','csv_rmse','csv_mae','csv_f1','csv_auc','csv_logloss'];
  var $checker = $('select[name=checker]');
  var $args = $('textarea[name=checker_args]');
  function render() {
    var v = $checker.val();
    var current = {};
    try { current = JSON.parse($args.val() || '{}'); } catch (e) {}
    if (csvKeys.indexOf(v) === -1) { $('#csv-helper').remove(); return; }
    if ($('#csv-helper').length === 0) {
      $args.after('<div id="csv-helper">' +
        '<label>id_column: <input id="csv-id" type="text"></label> ' +
        '<label>label_column: <input id="csv-lbl" type="text"></label> ' +
        '<label>has_header: <input id="csv-hdr" type="checkbox" checked></label>' +
        '</div>');
    }
    $('#csv-id').val(current.id_column || '');
    $('#csv-lbl').val(current.label_column || '');
    $('#csv-hdr').prop('checked', current.has_header !== false);
  }
  function sync() {
    if (csvKeys.indexOf($checker.val()) === -1) return;
    $args.val(JSON.stringify({
      id_column: $('#csv-id').val(),
      label_column: $('#csv-lbl').val(),
      has_header: $('#csv-hdr').is(':checked'),
    }));
  }
  $checker.on('change', render);
  $('form').on('submit', sync);
  $('#csv-helper input').on('change', sync);
  render();
})();
</script>
```

- [ ] **Step 6.8: Commit**

```bash
git add judge/models/problem_data.py judge/views/problem_data.py \
        judge/migrations/0237_alter_problemdata_checker.py \
        templates/problem/data.html judge/tests/test_problem_attachment.py
git commit -m "Add CSV checker keys + checker_args UI"
```

---

## Task 7: Submit-page presigned-upload path for output-only

**Files:**
- Modify: `judge/forms.py:198`
- Modify: `templates/problem/submit.html:5-41,184-199`
- Modify: `judge/views/direct_upload.py:29`

- [ ] **Step 7.1: Bump global submission size cap**

Modify `judge/forms.py:198`:

```python
def file_size_validator(file):
    limit = 50 * 1024 * 1024  # was 10 MB; output-only submissions can be large CSVs
    if file.size > limit:
        raise ValidationError("File too large. Size should not exceed 50MB.")
```

- [ ] **Step 7.2: Add rate limit to upload-token issuance**

Modify `judge/views/direct_upload.py:29` — wrap `get_upload_config`:

```python
from django_ratelimit.decorators import ratelimit

@ratelimit(key='user', rate='30/m', block=True)
@login_required
@require_POST
def get_upload_config(request):
    ...
```

If `django_ratelimit` is not installed, add it to `requirements.txt` and `pip install django-ratelimit`. (Verify first: `grep -n "django.ratelimit\|ratelimit" requirements.txt`.)

- [ ] **Step 7.3: Wire submit page to direct_upload for OUTPUT language**

Modify `templates/problem/submit.html:184-199` — replace the existing JSZip wrapping block. The new logic: if file is >10 MB and storage is S3-like, request an upload token + presigned URL, PUT/POST the file to S3, then submit the form with the resulting key in a hidden field. Otherwise fall back to existing multipart submission.

Replace the JSZip block with:

```javascript
// templates/problem/submit.html
async function submitOutputOnly(form, fileInput) {
    var files = fileInput.files;
    if (files.length === 0) return true;  // browser will block on required field
    var file = files.length === 1 ? files[0] : await zipFiles(files); // existing zipFiles helper

    if (file.size <= 10 * 1024 * 1024) {
        // Existing path: multipart through Django
        var dt = new DataTransfer(); dt.items.add(file);
        fileInput.files = dt.files;
        return true;
    }

    // Big file: presigned direct upload
    var tokenResp = await fetch('{{ url("submission_upload_token") }}', {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfToken(), 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
    });
    var token = (await tokenResp.json()).token;

    var configResp = await fetch('{{ url("get_upload_config") }}', {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfToken(), 'Content-Type': 'application/json' },
        body: JSON.stringify({
            upload_token: token,
            filename: file.name,
            content_type: file.type || 'application/octet-stream',
            file_size: file.size,
        }),
    });
    var config = await configResp.json();

    if (config.method === 'POST') {
        var fd = new FormData();
        Object.entries(config.fields || {}).forEach(([k, v]) => fd.append(k, v));
        fd.append('file', file);
        await fetch(config.upload_url, { method: 'POST', body: fd });
    } else {
        await fetch(config.upload_url, { method: 'PUT', body: file });
    }

    // Stash the key on the form so the submit handler reads it
    var hidden = document.createElement('input');
    hidden.type = 'hidden';
    hidden.name = 'uploaded_file_key';
    hidden.value = config.file_key;
    form.appendChild(hidden);

    // Clear the file input so multipart payload is empty
    fileInput.value = '';
    return true;
}
```

- [ ] **Step 7.4: Add server-side handling of `uploaded_file_key`**

Modify `judge/forms.py` — extend `ProblemSubmitForm.clean` (around line 236):

```python
def clean(self):
    uploaded_key = self.data.get('uploaded_file_key')
    if uploaded_key:
        # Trust the key only if it lives under the expected prefix
        if not uploaded_key.startswith('submissions/'):
            raise ValidationError("Invalid uploaded_file_key.")
        self.cleaned_data["source"] = self.request.build_absolute_uri(
            reverse("submission_source_file", args=(uploaded_key.removeprefix('submissions/'),))
        )
        return self.cleaned_data
    # existing source_file logic ...
```

Also add a new view `submission_upload_token` that issues an `upload_token` scoped to `upload_to='submissions/'`, `max_size=50*1024*1024`. Pattern after the existing `direct_upload.py` token issuance — see `judge/widgets/direct_upload.py` for `get_upload_token_data`.

Add to `judge/views/submission.py`:

```python
from judge.widgets.direct_upload import create_upload_token

@login_required
@require_POST
def submission_upload_token(request):
    token = create_upload_token(
        profile_id=request.profile.id,
        upload_to='submissions/',
        max_size=50 * 1024 * 1024,
        prefix='',
        object_id=None,
    )
    return JsonResponse({'token': token})
```

(If `create_upload_token` doesn't exist, study `judge/widgets/direct_upload.py` to find the actual factory; the `get_upload_token_data` reverse implies a `set_upload_token_data` or constructor — name the wrapper accordingly.)

Wire URL in `dmoj/urls.py`:

```python
re_path(r'^submission/upload_token$', submission_upload_token, name='submission_upload_token'),
```

- [ ] **Step 7.5: Manual smoke test**

```bash
./make_style.sh && python3 manage.py collectstatic --noinput
python3 manage.py runserver 0.0.0.0:8000
```

In a browser:
1. Open an output-only problem submit page.
2. Upload a small (<10 MB) file → verify multipart path still works (submission appears).
3. Upload a large (>10 MB, e.g. 30 MB CSV) file → verify presigned path: file appears in S3, submission is created with the right URL in `SubmissionSource.source`.

- [ ] **Step 7.6: Commit**

```bash
git add judge/forms.py judge/views/direct_upload.py judge/views/submission.py \
        templates/problem/submit.html dmoj/urls.py
git commit -m "Wire presigned direct-upload for output-only submissions"
```

---

## Task 8: Polish — leaderboard link, dark mode, format hint

**Files:**
- Modify: `templates/problem/problem.html`
- Modify: `resources/problem_attachments.scss`

- [ ] **Step 8.1: Add Leaderboard link on problem page**

Modify `templates/problem/problem.html` — inside the action area where "Submit" / "Submissions" links live, add a conditional:

```jinja
{% if problem.data_files and problem.data_files.output_only %}
<a href="{{ url('ranked_submissions', problem.code) }}" class="leaderboard-link">
  <i class="fa fa-trophy"></i> {{ _('Leaderboard') }}
</a>
{% endif %}
```

(Verify URL name with `grep -n "ranked_submissions\|RankedSubmissions" dmoj/urls.py`.)

- [ ] **Step 8.2: Verify dark mode**

Open `http://localhost:8000/problem/<code>/attachments/` and the problem page in dark mode. Confirm no hardcoded colors. Check `resources/problem_attachments.scss` uses only SCSS variables (it does — verify in step 4).

- [ ] **Step 8.3: Commit**

```bash
git add templates/problem/problem.html
git commit -m "Add leaderboard link on output-only problem pages"
```

---

## Task 9: Playwright smoke test

**Files:**
- Create: `tmp/test-attachments.png` (and other screenshots — gitignored)

This is a manual verification gate; not committed.

- [ ] **Step 9.1: Start the server**

```bash
celery -A dmoj_celery worker &
python3 manage.py runserver 0.0.0.0:8000 &
```

- [ ] **Step 9.2: Drive the flow with Playwright MCP**

In an interactive Claude session, use Playwright MCP to:
1. Log in as a problem author.
2. Navigate to `/problem/<code>/attachments/`. Screenshot to `tmp/attachments-empty.png`.
3. Upload a small CSV. Screenshot post-upload to `tmp/attachments-with-file.png`.
4. Reorder (drag the row), screenshot.
5. Open the problem page; verify the "Files" section shows the attachment. Screenshot to `tmp/problem-with-attachments.png`.
6. Switch to a solver account; submit a >10MB file to an output-only problem; verify the network tab shows a direct PUT/POST to S3 (not Django). Screenshot the submission detail page.
7. Toggle dark mode; re-screenshot the attachments tab and problem page.

- [ ] **Step 9.3: Document any regressions**

If anything breaks, file follow-up tasks. Otherwise, no commit.

---

---

## Task 10: Judge-server CSV checker implementations

**Repo:** `~/LQDOJ/judge-server` (separate from the site repo).

**Files:**
- Create: `dmoj/checkers/csv_accuracy.py`
- Create: `dmoj/checkers/csv_rmse.py`
- Create: `dmoj/checkers/csv_mae.py`
- Create: `dmoj/checkers/csv_f1.py`
- Create: `dmoj/checkers/csv_auc.py`
- Create: `dmoj/checkers/csv_logloss.py`
- Create: `dmoj/checkers/_csv_common.py` — shared CSV-loading + arg-parsing helpers
- Test: `dmoj/tests/test_csv_checkers.py`

**Pattern note:** existing checkers (e.g. `dmoj/checkers/floatsrel.py`) export a `check(process_output: bytes, judge_output: bytes, point_value: float, **kwargs) -> CheckerResult` function. `kwargs` receives the JSON `checker_args` from init.yml as keyword arguments. We follow that pattern exactly.

**Score normalization:** all checkers return `points ∈ [0, point_value]`. For higher-better metrics (accuracy/F1/AUC), the metric value in [0,1] scales `point_value`. For lower-better metrics (RMSE/MAE/logloss), normalize via `1/(1 + value)` so `value=0 → score=1`, `value→∞ → score→0`. The author can tune by setting baseline points.

- [ ] **Step 10.1: Write the failing tests**

Create `dmoj/tests/test_csv_checkers.py`:

```python
import unittest

from dmoj.checkers.csv_accuracy import check as accuracy_check
from dmoj.checkers.csv_rmse import check as rmse_check
from dmoj.checkers.csv_mae import check as mae_check
from dmoj.checkers.csv_f1 import check as f1_check
from dmoj.checkers.csv_auc import check as auc_check
from dmoj.checkers.csv_logloss import check as logloss_check


JUDGE = b"id,y\n1,1\n2,0\n3,1\n4,0\n"


class CsvAccuracyTests(unittest.TestCase):
    def test_perfect_match(self):
        sub = b"id,y\n1,1\n2,0\n3,1\n4,0\n"
        r = accuracy_check(sub, JUDGE, 100.0,
                           has_header=True, id_column='id', label_column='y')
        self.assertAlmostEqual(r.points, 100.0)

    def test_half_correct(self):
        sub = b"id,y\n1,1\n2,1\n3,1\n4,1\n"  # 2/4 right
        r = accuracy_check(sub, JUDGE, 100.0,
                           has_header=True, id_column='id', label_column='y')
        self.assertAlmostEqual(r.points, 50.0)

    def test_missing_id_in_submission(self):
        sub = b"id,y\n1,1\n2,0\n3,1\n"  # row 4 missing
        r = accuracy_check(sub, JUDGE, 100.0,
                           has_header=True, id_column='id', label_column='y')
        # Treat missing rows as wrong → 3/4 = 75
        self.assertAlmostEqual(r.points, 75.0)


class CsvRmseTests(unittest.TestCase):
    JUDGE_NUM = b"id,y\n1,0\n2,0\n3,0\n4,0\n"

    def test_zero_error_full_score(self):
        sub = b"id,y\n1,0\n2,0\n3,0\n4,0\n"
        r = rmse_check(sub, self.JUDGE_NUM, 100.0,
                       has_header=True, id_column='id', label_column='y')
        self.assertAlmostEqual(r.points, 100.0)

    def test_higher_error_lower_score(self):
        sub_small = b"id,y\n1,0.1\n2,0.1\n3,0.1\n4,0.1\n"
        sub_big = b"id,y\n1,5\n2,5\n3,5\n4,5\n"
        r1 = rmse_check(sub_small, self.JUDGE_NUM, 100.0,
                        has_header=True, id_column='id', label_column='y')
        r2 = rmse_check(sub_big, self.JUDGE_NUM, 100.0,
                        has_header=True, id_column='id', label_column='y')
        self.assertGreater(r1.points, r2.points)


class CsvMaeTests(unittest.TestCase):
    def test_zero_error(self):
        judge = b"id,y\n1,2\n2,4\n"
        sub = b"id,y\n1,2\n2,4\n"
        r = mae_check(sub, judge, 100.0,
                      has_header=True, id_column='id', label_column='y')
        self.assertAlmostEqual(r.points, 100.0)


class CsvF1Tests(unittest.TestCase):
    def test_perfect(self):
        judge = b"id,y\n1,a\n2,b\n3,a\n4,b\n"
        sub = b"id,y\n1,a\n2,b\n3,a\n4,b\n"
        r = f1_check(sub, judge, 100.0,
                     has_header=True, id_column='id', label_column='y')
        self.assertAlmostEqual(r.points, 100.0, places=4)


class CsvAucTests(unittest.TestCase):
    def test_perfect_separation(self):
        judge = b"id,y\n1,0\n2,0\n3,1\n4,1\n"
        sub = b"id,y\n1,0.1\n2,0.2\n3,0.8\n4,0.9\n"
        r = auc_check(sub, judge, 100.0,
                      has_header=True, id_column='id', label_column='y')
        self.assertAlmostEqual(r.points, 100.0, places=4)


class CsvLoglossTests(unittest.TestCase):
    def test_confident_correct(self):
        judge = b"id,y\n1,0\n2,1\n"
        sub_good = b"id,y\n1,0.01\n2,0.99\n"
        sub_bad  = b"id,y\n1,0.99\n2,0.01\n"
        rg = logloss_check(sub_good, judge, 100.0,
                           has_header=True, id_column='id', label_column='y')
        rb = logloss_check(sub_bad, judge, 100.0,
                           has_header=True, id_column='id', label_column='y')
        self.assertGreater(rg.points, rb.points)


class CsvCheckerErrorTests(unittest.TestCase):
    def test_malformed_submission_returns_zero(self):
        sub = b"this is not csv at all"
        r = accuracy_check(sub, JUDGE, 100.0,
                           has_header=True, id_column='id', label_column='y')
        self.assertEqual(r.points, 0)


class CsvPretestModeTests(unittest.TestCase):
    """When _pretests_only=False (default), pretest_fraction is ignored.
    When _pretests_only=True, pretest_fraction filters rows by id hash."""

    JUDGE_BIG = b"id,y\n" + b"".join(f"{i},{i % 2}\n".encode() for i in range(100))

    def test_full_eval_when_not_in_pretests_mode(self):
        # All rows correct
        sub = self.JUDGE_BIG
        r = accuracy_check(sub, self.JUDGE_BIG, 100.0,
                           has_header=True, id_column='id', label_column='y',
                           pretest_fraction=0.5, _pretests_only=False)
        # Full eval ignores fraction → 100/100 correct → score 100
        self.assertAlmostEqual(r.points, 100.0)

    def test_partial_eval_when_in_pretests_mode(self):
        # Submission identical to judge → still 100% on whichever rows are selected
        sub = self.JUDGE_BIG
        r = accuracy_check(sub, self.JUDGE_BIG, 100.0,
                           has_header=True, id_column='id', label_column='y',
                           pretest_fraction=0.5, _pretests_only=True)
        # Even with subset, all selected rows match → score 100
        self.assertAlmostEqual(r.points, 100.0)

    def test_pretest_subset_is_deterministic(self):
        # Run twice with the same fraction; same rows must be selected
        from dmoj.checkers._csv_common import parse_csv
        a = parse_csv(self.JUDGE_BIG, has_header=True, id_column='id',
                      label_column='y', pretest_fraction=0.5, _filter_ids=True)
        b = parse_csv(self.JUDGE_BIG, has_header=True, id_column='id',
                      label_column='y', pretest_fraction=0.5, _filter_ids=True)
        self.assertEqual(set(a.keys()), set(b.keys()))
        # Should pick roughly half (allow some variance)
        self.assertGreater(len(a), 30)
        self.assertLess(len(a), 70)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 10.2: Run, expect failure**

```bash
cd ~/LQDOJ/judge-server
python3 -m pytest dmoj/tests/test_csv_checkers.py -v
```
Expected: `ImportError: No module named 'dmoj.checkers.csv_accuracy'`.

- [ ] **Step 10.3: Implement common helpers**

Create `~/LQDOJ/judge-server/dmoj/checkers/_csv_common.py`:

```python
"""
Common helpers for csv_* checkers. Each checker takes:
  process_output (submission), judge_output (answer key) — both bytes.
  kwargs (from init.yml's checker_args):
      has_header: bool (default True)
      id_column: str (required)
      label_column: str (required)
      pretest_fraction: float in (0, 1] (default 1.0) — fraction of judge rows
          included in scoring when the *judge* is running in pretests-only mode
          (i.e. contest with run_pretests_only=True and the problem's testcase
          marked is_pretest=True). Outside pretest mode, this arg is ignored
          and 100% of rows are scored. The judge passes its pretest mode in via
          the magic `_pretests_only` kwarg (set by Problem.checker() in
          judge-server).
"""
import csv
import hashlib
import io
from typing import Dict, Tuple


def _row_in_pretest(row_id: str, fraction: float) -> bool:
    if fraction >= 1.0:
        return True
    if fraction <= 0.0:
        return False
    h = hashlib.md5(row_id.encode('utf-8')).digest()
    bucket = int.from_bytes(h[:4], 'big') % 1000
    return bucket < int(fraction * 1000)


def parse_csv(
    blob: bytes,
    has_header: bool,
    id_column: str,
    label_column: str,
    pretest_fraction: float = 1.0,
    _filter_ids: bool = False,
) -> Dict[str, str]:
    """Return {id: label_string}. Skips rows missing either column.
    If _filter_ids=True, also drops rows whose id is not in the active pretest subset.
    """
    text = blob.decode('utf-8', errors='replace')
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return {}

    if has_header:
        header = rows[0]
        try:
            id_idx = header.index(id_column)
            label_idx = header.index(label_column)
        except ValueError:
            return {}
        data_rows = rows[1:]
    else:
        try:
            id_idx = int(id_column)
            label_idx = int(label_column)
        except ValueError:
            return {}
        data_rows = rows

    out = {}
    for row in data_rows:
        if len(row) <= max(id_idx, label_idx):
            continue
        rid = row[id_idx]
        if _filter_ids and not _row_in_pretest(rid, pretest_fraction):
            continue
        out[rid] = row[label_idx]
    return out


def aligned_pairs(judge_blob: bytes, sub_blob: bytes, **kwargs) -> Tuple[list, list, int, int]:
    """
    Returns (judge_labels, sub_labels, total, missing_in_sub).
    For ids missing in submission, sub_label is None.
    For ids only in submission (extra), they're ignored.
    The pretest fraction is applied to the judge side: only ids selected by
    the hash filter contribute to scoring. Submission rows for ids outside
    the active subset are simply ignored.
    """
    judge_kwargs = dict(kwargs); judge_kwargs['_filter_ids'] = True
    sub_kwargs = dict(kwargs); sub_kwargs['_filter_ids'] = False
    judge = parse_csv(judge_blob, **judge_kwargs)
    sub = parse_csv(sub_blob, **sub_kwargs)
    judge_labels, sub_labels = [], []
    missing = 0
    for k, v in judge.items():
        judge_labels.append(v)
        sv = sub.get(k)
        sub_labels.append(sv)
        if sv is None:
            missing += 1
    return judge_labels, sub_labels, len(judge), missing


def feedback(metric_name: str, value: float, fraction: float = 1.0) -> str:
    suffix = f' (public LB on {int(fraction * 100)}% of rows)' if fraction < 1.0 else ''
    return f'{metric_name} = {value:.6f}{suffix}'
```

- [ ] **Step 10.4: Implement `csv_accuracy`**

Create `~/LQDOJ/judge-server/dmoj/checkers/csv_accuracy.py`:

```python
from dmoj.checkers._csv_common import aligned_pairs, feedback
from dmoj.result import CheckerResult


def check(process_output: bytes, judge_output: bytes, point_value: float,
          has_header: bool = True, id_column: str = 'id', label_column: str = 'y',
          pretest_fraction: float = 1.0,
          _pretests_only: bool = False,
          **kwargs) -> CheckerResult:
    try:
        j, s, total, _ = aligned_pairs(
            judge_output, process_output,
            has_header=has_header, id_column=id_column, label_column=label_column,
            pretest_fraction=pretest_fraction if _pretests_only else 1.0,
        )
        if total == 0:
            return CheckerResult(False, 0, feedback='empty answer key')
        correct = sum(1 for a, b in zip(j, s) if a == b and b is not None)
        acc = correct / total
        return CheckerResult(acc > 0, point_value * acc, feedback=feedback('accuracy', acc))
    except Exception as e:
        return CheckerResult(False, 0, feedback=f'checker error: {e!r}')
```

- [ ] **Step 10.5: Implement `csv_rmse`**

Create `~/LQDOJ/judge-server/dmoj/checkers/csv_rmse.py`:

```python
import math
from dmoj.checkers._csv_common import aligned_pairs, feedback
from dmoj.result import CheckerResult


def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def check(process_output: bytes, judge_output: bytes, point_value: float,
          has_header: bool = True, id_column: str = 'id', label_column: str = 'y',
          pretest_fraction: float = 1.0,
          _pretests_only: bool = False,
          **kwargs) -> CheckerResult:
    try:
        j, s, total, _ = aligned_pairs(
            judge_output, process_output,
            has_header=has_header, id_column=id_column, label_column=label_column,
            pretest_fraction=pretest_fraction if _pretests_only else 1.0,
        )
        if total == 0:
            return CheckerResult(False, 0, feedback='empty answer key')
        sq = 0.0
        for a, b in zip(j, s):
            af = _to_float(a)
            bf = _to_float(b) if b is not None else None
            if af is None:
                continue
            if bf is None:
                # Penalty: treat missing as the worst-case error of 1e9
                bf = af + 1e9
            sq += (af - bf) ** 2
        rmse = math.sqrt(sq / total)
        score = 1.0 / (1.0 + rmse)
        return CheckerResult(score > 0, point_value * score, feedback=feedback('RMSE', rmse))
    except Exception as e:
        return CheckerResult(False, 0, feedback=f'checker error: {e!r}')
```

- [ ] **Step 10.6: Implement `csv_mae`**

Create `~/LQDOJ/judge-server/dmoj/checkers/csv_mae.py`:

```python
from dmoj.checkers._csv_common import aligned_pairs, feedback
from dmoj.result import CheckerResult


def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def check(process_output: bytes, judge_output: bytes, point_value: float,
          has_header: bool = True, id_column: str = 'id', label_column: str = 'y',
          pretest_fraction: float = 1.0,
          _pretests_only: bool = False,
          **kwargs) -> CheckerResult:
    try:
        j, s, total, _ = aligned_pairs(
            judge_output, process_output,
            has_header=has_header, id_column=id_column, label_column=label_column,
            pretest_fraction=pretest_fraction if _pretests_only else 1.0,
        )
        if total == 0:
            return CheckerResult(False, 0, feedback='empty answer key')
        absum = 0.0
        for a, b in zip(j, s):
            af = _to_float(a)
            bf = _to_float(b) if b is not None else None
            if af is None:
                continue
            if bf is None:
                bf = af + 1e9
            absum += abs(af - bf)
        mae = absum / total
        score = 1.0 / (1.0 + mae)
        return CheckerResult(score > 0, point_value * score, feedback=feedback('MAE', mae))
    except Exception as e:
        return CheckerResult(False, 0, feedback=f'checker error: {e!r}')
```

- [ ] **Step 10.7: Implement `csv_f1` (macro)**

Create `~/LQDOJ/judge-server/dmoj/checkers/csv_f1.py`:

```python
from collections import defaultdict
from dmoj.checkers._csv_common import aligned_pairs, feedback
from dmoj.result import CheckerResult


def _macro_f1(true_labels, pred_labels):
    classes = set(true_labels) | set(p for p in pred_labels if p is not None)
    if not classes:
        return 0.0
    f1s = []
    for c in classes:
        tp = sum(1 for t, p in zip(true_labels, pred_labels) if t == c and p == c)
        fp = sum(1 for t, p in zip(true_labels, pred_labels) if t != c and p == c)
        fn = sum(1 for t, p in zip(true_labels, pred_labels) if t == c and p != c)
        if tp == 0:
            f1s.append(0.0)
            continue
        prec = tp / (tp + fp)
        rec = tp / (tp + fn)
        f1s.append(2 * prec * rec / (prec + rec) if (prec + rec) else 0.0)
    return sum(f1s) / len(f1s)


def check(process_output: bytes, judge_output: bytes, point_value: float,
          has_header: bool = True, id_column: str = 'id', label_column: str = 'y',
          pretest_fraction: float = 1.0,
          _pretests_only: bool = False,
          **kwargs) -> CheckerResult:
    try:
        j, s, total, _ = aligned_pairs(
            judge_output, process_output,
            has_header=has_header, id_column=id_column, label_column=label_column,
            pretest_fraction=pretest_fraction if _pretests_only else 1.0,
        )
        if total == 0:
            return CheckerResult(False, 0, feedback='empty answer key')
        score = _macro_f1(j, s)
        return CheckerResult(score > 0, point_value * score, feedback=feedback('F1_macro', score))
    except Exception as e:
        return CheckerResult(False, 0, feedback=f'checker error: {e!r}')
```

- [ ] **Step 10.8: Implement `csv_auc`**

Create `~/LQDOJ/judge-server/dmoj/checkers/csv_auc.py`:

```python
from dmoj.checkers._csv_common import aligned_pairs, feedback
from dmoj.result import CheckerResult


def _binary_auc(y_true, y_score):
    pairs = [(s, t) for s, t in zip(y_score, y_true) if s is not None and t is not None]
    if not pairs:
        return 0.0
    pos = [s for s, t in pairs if t == 1]
    neg = [s for s, t in pairs if t == 0]
    if not pos or not neg:
        return 0.0
    wins = ties = 0
    for p in pos:
        for n in neg:
            if p > n: wins += 1
            elif p == n: ties += 1
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def check(process_output: bytes, judge_output: bytes, point_value: float,
          has_header: bool = True, id_column: str = 'id', label_column: str = 'y',
          pretest_fraction: float = 1.0,
          _pretests_only: bool = False,
          **kwargs) -> CheckerResult:
    try:
        j_raw, s_raw, total, _ = aligned_pairs(
            judge_output, process_output,
            has_header=has_header, id_column=id_column, label_column=label_column,
        )
        if total == 0:
            return CheckerResult(False, 0, feedback='empty answer key')
        y_true = [int(v) if v in ('0', '1') else None for v in j_raw]
        y_score = []
        for v in s_raw:
            try:
                y_score.append(float(v) if v is not None else None)
            except (TypeError, ValueError):
                y_score.append(None)
        auc = _binary_auc(y_true, y_score)
        return CheckerResult(auc > 0, point_value * auc, feedback=feedback('AUC', auc))
    except Exception as e:
        return CheckerResult(False, 0, feedback=f'checker error: {e!r}')
```

- [ ] **Step 10.9: Implement `csv_logloss`**

Create `~/LQDOJ/judge-server/dmoj/checkers/csv_logloss.py`:

```python
import math
from dmoj.checkers._csv_common import aligned_pairs, feedback
from dmoj.result import CheckerResult

EPS = 1e-15


def check(process_output: bytes, judge_output: bytes, point_value: float,
          has_header: bool = True, id_column: str = 'id', label_column: str = 'y',
          pretest_fraction: float = 1.0,
          _pretests_only: bool = False,
          **kwargs) -> CheckerResult:
    try:
        j_raw, s_raw, total, _ = aligned_pairs(
            judge_output, process_output,
            has_header=has_header, id_column=id_column, label_column=label_column,
        )
        if total == 0:
            return CheckerResult(False, 0, feedback='empty answer key')
        loss_sum = 0.0
        n = 0
        for tv, sv in zip(j_raw, s_raw):
            try:
                t = int(tv)
            except (TypeError, ValueError):
                continue
            try:
                p = float(sv) if sv is not None else 0.5
            except (TypeError, ValueError):
                p = 0.5
            p = max(EPS, min(1.0 - EPS, p))
            loss_sum += -(t * math.log(p) + (1 - t) * math.log(1 - p))
            n += 1
        if n == 0:
            return CheckerResult(False, 0, feedback='no valid rows')
        ll = loss_sum / n
        score = 1.0 / (1.0 + ll)
        return CheckerResult(score > 0, point_value * score, feedback=feedback('logloss', ll))
    except Exception as e:
        return CheckerResult(False, 0, feedback=f'checker error: {e!r}')
```

- [ ] **Step 10.9b: Patch judge-server to thread pretest mode into checkers**

The checker invocation at `~/LQDOJ/judge-server/dmoj/problem.py:528` builds a `partial(checker.check, **params)`. Modify it to inject the problem's pretest mode so checkers can gate behavior on it. Find the `checker(self) -> partial:` method (around line 500) and just before `return partial(checker.check, **params)` add:

```python
        params['_pretests_only'] = self.problem.run_pretests_only
```

This is safe for all existing checkers because their `check()` signatures use `**kwargs` and ignore unknown args.

Verification: run the existing judge-server test suite to confirm nothing breaks:

```bash
cd ~/LQDOJ/judge-server
python3 -m pytest dmoj/tests/ -v
```
Expected: pre-existing tests still pass.

- [ ] **Step 10.10: Run all tests, expect pass**

```bash
cd ~/LQDOJ/judge-server
python3 -m pytest dmoj/tests/test_csv_checkers.py -v
```
Expected: all tests pass.

- [ ] **Step 10.11: Commit**

```bash
cd ~/LQDOJ/judge-server
git add dmoj/checkers/_csv_common.py dmoj/checkers/csv_*.py dmoj/tests/test_csv_checkers.py
git commit -m "Add CSV scoring checkers (accuracy, RMSE, MAE, F1, AUC, log-loss)"
```

- [ ] **Step 10.12: Restart judge nodes to pick up the new checkers**

```bash
# Manual: restart whichever supervisord/systemd unit runs the local judge
# For dev:
pkill -f "dmoj-cli\|dmoj.run_judge" || true
# Then re-launch via the project's normal "Run Judge" command (.claude/commands/run-judge.md)
```

End-to-end smoke: configure a problem with `csv_rmse`, submit a CSV, watch the score appear with `RMSE = X` in the feedback.

---

---

## Task 11: Public / Private leaderboard via `pretest_fraction` (UI nudge + workflow doc)

**Files:**
- Modify: the problem-data edit template (verify path — likely `templates/problem/data.html`)
- Modify: `resources/problem_attachments.scss`

**Mechanism summary:** the row-level split is implemented inside the `csv_*` checkers (Task 10's `_csv_common.py`) via the `pretest_fraction` checker arg. **Whether it applies is automatic** — the judge-server patch in Task 10.9b passes `_pretests_only=True` into the checker iff the contest is in `run_pretests_only` mode, which is the existing contest pretest lifecycle.

**Author workflow (just standard contest setup, plus one extra arg):**

1. Configure the problem with `csv_*` checker, set `pretest_fraction: 0.5` in checker_args.
2. Mark the (single) test case `is_pretest: True`.
3. In the contest: set `ContestProblem.is_pretested = True` and `Contest.run_pretests_only = True`.

That's it. During the contest, the judge runs in pretest mode → checker honors the fraction → public LB. After contest ends, contest setter flips `Contest.run_pretests_only = False` (standard practice) and rejudges → checker sees `_pretests_only=False` → ignores fraction → full eval → private LB scores revealed.

This task is mostly UX guidance — the engine work is done in Task 10. Concretely:
- Extend the CSV-args helper in Task 6.7's template to expose `pretest_fraction` as a number input alongside `id_column`/`label_column`.
- Add an info banner that explains the contest-pretest-mode integration.

- [ ] **Step 11.1: Extend the CSV-args UI helper to include `pretest_fraction`**

Modify the `<script>` block added in Task 6.7 (in the problem-data edit template). Replace its body with:

```javascript
(function() {
  var csvKeys = ['csv_accuracy','csv_rmse','csv_mae','csv_f1','csv_auc','csv_logloss'];
  var $checker = $('select[name=checker]');
  var $args = $('textarea[name=checker_args]');

  function render() {
    var v = $checker.val();
    var current = {};
    try { current = JSON.parse($args.val() || '{}'); } catch (e) {}
    if (csvKeys.indexOf(v) === -1) { $('#csv-helper').remove(); return; }
    if ($('#csv-helper').length === 0) {
      $args.after('<div id="csv-helper">' +
        '<label>id_column: <input id="csv-id" type="text"></label> ' +
        '<label>label_column: <input id="csv-lbl" type="text"></label> ' +
        '<label>has_header: <input id="csv-hdr" type="checkbox" checked></label> ' +
        '<label>pretest_fraction: <input id="csv-pf" type="number" min="0" max="1" step="0.05"></label>' +
        '</div>');
    }
    $('#csv-id').val(current.id_column || '');
    $('#csv-lbl').val(current.label_column || '');
    $('#csv-hdr').prop('checked', current.has_header !== false);
    $('#csv-pf').val(current.pretest_fraction != null ? current.pretest_fraction : 1.0);
  }

  function sync() {
    if (csvKeys.indexOf($checker.val()) === -1) return;
    var pf = parseFloat($('#csv-pf').val());
    if (!isFinite(pf) || pf <= 0) pf = 1.0;
    if (pf > 1) pf = 1.0;
    var args = {
      id_column: $('#csv-id').val(),
      label_column: $('#csv-lbl').val(),
      has_header: $('#csv-hdr').is(':checked'),
    };
    if (pf < 1.0) args.pretest_fraction = pf;
    $args.val(JSON.stringify(args));
  }

  $checker.on('change', render);
  $('form').on('submit', sync);
  $(document).on('change', '#csv-helper input', sync);
  render();
})();
```

- [ ] **Step 11.2: Add the workflow info banner**

In the same template, above the CSV helper, add (rendered conditionally when checker is `csv_*`):

```jinja
<div class="alert info pretest-hint" id="csv-pretest-hint" style="display:none">
  <strong>{{ _('Public / Private leaderboard workflow:') }}</strong>
  <ul style="margin: 0.3em 0 0 1.2em;">
    <li>{{ _('Set pretest_fraction to e.g. 0.5 — solvers will see scores on a hash-selected 50%% of rows (the public LB) only when the contest is running in pretests-only mode.') | safe }}</li>
    <li>{{ _('Mark the test case as is_pretest in the test data editor, and set the contest problem as is_pretested with run_pretests_only=True on the contest.') }}</li>
    <li>{{ _('After the contest ends, the contest setter flips run_pretests_only=False and rejudges — the checker then ignores pretest_fraction and scores all rows (the private LB).') }}</li>
    <li>{{ _('Row selection is deterministic by row id (md5 hash) — the same subset is used for every submission, so the public leaderboard is fair.') }}</li>
  </ul>
</div>
```

Wire the show/hide to the checker dropdown — append to the `<script>` block:

```javascript
function toggleHint() {
  var v = $checker.val();
  $('#csv-pretest-hint').toggle(csvKeys.indexOf(v) !== -1);
}
$checker.on('change', toggleHint);
toggleHint();
```

- [ ] **Step 11.3: Style the banner**

Append to `resources/problem_attachments.scss`:

```scss
.pretest-hint {
    background: $background_light_gray;
    color: $widget_black;
    padding: 0.7em 1em;
    margin: 1em 0;
    border-left: 3px solid $theme_color;
    border-radius: 4px;

    ul { margin-bottom: 0; }
}

#csv-helper {
    margin-top: 0.5em;
    padding: 0.5em;
    background: $background_light_gray;
    border-radius: 4px;

    label { display: inline-block; margin-right: 1em; }
}
```

Recompile:

```bash
./make_style.sh && python3 manage.py collectstatic --noinput
```

- [ ] **Step 11.4: Manual smoke test**

1. Configure a problem with `csv_accuracy`, set `pretest_fraction=0.5`. Mark the test case `is_pretest=True`.
2. Add the problem to a contest with `is_pretested=True` and `run_pretests_only=True` on the contest.
3. While contest is live, submit a CSV → score should reflect ~50% of rows.
4. Stop the contest (or flip `run_pretests_only=False`), rejudge → score reflects all rows.

- [ ] **Step 11.5: Commit**

```bash
git add templates/problem/data.html resources/problem_attachments.scss
git commit -m "Add pretest_fraction UI helper and workflow hint for CSV checkers"
```

---

## Self-review checklist

- [x] Spec §3.2 (ProblemAttachment model) → Task 1.
- [x] Spec §3.3 (CSV checker keys + checker_args) → Task 6 (impl in judge-server is out of scope, noted).
- [x] Spec §3.4 (attachment management + download) → Tasks 3, 4, 5.
- [x] Spec §3.5 (presigned submit upload + ratelimit) → Task 7.
- [x] Spec §3.6 (leaderboard link as polish) → Task 8.
- [x] Spec build-order steps 1, 2 (attachments) → Tasks 1–5.
- [x] Spec build-order step 3 (CSV keys) → Task 6.
- [x] Spec build-order step 4 (submit upgrade) → Task 7.
- [x] Spec build-order step 5 (polish) → Tasks 8, 9.
- [x] Judge-server CSV checker implementations → Task 10.
- [x] Public/Private leaderboard split via `is_pretest` → Task 11.

**Outstanding caveats (acknowledged in plan, not gaps):**
- The `csv_*` checker implementations live in the judge-server fork — separate plan in that repo. Problems configured with `csv_*` will not grade until that ships.
- Step 7.4 references `create_upload_token` but the exact name should be confirmed against `judge/widgets/direct_upload.py` during implementation.
- Step 6.7 references `templates/problem/data.html` — confirm exact path with `grep -rn "checker_args" templates/`.
