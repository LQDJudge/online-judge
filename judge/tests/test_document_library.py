from io import BytesIO
from unittest.mock import patch
from urllib.parse import quote

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client
from django.urls import reverse

from judge.models import Profile, Language


class LibraryReaderBackendTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        cache.clear()

    def test_raw_serves_pdf_inline(self):
        with patch(
            "judge.views.document_library.storage_file_exists", return_value=True
        ), patch("judge.views.document_library.default_storage") as mock_storage:
            mock_storage.open.return_value = BytesIO(b"%PDF-1.4 hello")
            url = reverse("library_raw", args=["Math/Algebra/book.pdf"])
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("inline", resp["Content-Disposition"])

    def test_raw_non_pdf_404(self):
        with patch(
            "judge.views.document_library.storage_file_exists", return_value=True
        ):
            resp = self.client.get(reverse("library_raw", args=["Math/notes.txt"]))
        self.assertEqual(resp.status_code, 404)

    def test_raw_missing_404(self):
        with patch(
            "judge.views.document_library.storage_file_exists", return_value=False
        ):
            resp = self.client.get(reverse("library_raw", args=["Math/ghost.pdf"]))
        self.assertEqual(resp.status_code, 404)

    def test_traversal_404(self):
        # Escaping the library root must be rejected.
        resp = self.client.get("/library/raw/../../etc/passwd.pdf")
        self.assertIn(resp.status_code, (400, 404))

    def test_document_page_renders_reader(self):
        with patch(
            "judge.views.document_library.storage_file_exists", return_value=True
        ):
            url = reverse("library_document", args=["Math/Algebra/book.pdf"])
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        # ManifestStaticFilesStorage fingerprints the filename (viewer.<hash>.html),
        # so assert on the stable path prefix, not the exact file name.
        self.assertIn(b"libs/pdfjs/web/viewer", resp.content)
        raw_url = reverse("library_raw", args=["Math/Algebra/book.pdf"])
        self.assertTrue(
            raw_url.encode() in resp.content
            or quote(raw_url, safe="").encode() in resp.content
        )


class LibraryBrowseTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        cache.clear()

    def test_root_lists_folders_and_files(self):
        # The grid lives at /library/browse/ (root is now the catalog).
        with patch(
            "judge.views.document_library.storage_listdir",
            return_value=(["Math", "Reference"], ["intro.pdf", ".keep"]),
        ):
            resp = self.client.get(reverse("library_browse", args=[""]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(reverse("library_browse", args=["Math"]).encode(), resp.content)
        self.assertIn(
            reverse("library_document", args=["intro.pdf"]).encode(), resp.content
        )
        self.assertNotIn(b".keep", resp.content)

    def test_subfolder_breadcrumbs_and_pdf_link(self):
        with patch(
            "judge.views.document_library.storage_listdir",
            return_value=(["Algebra"], ["book.pdf"]),
        ):
            resp = self.client.get(reverse("library_browse", args=["Math"]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            reverse("library_document", args=["Math/book.pdf"]).encode(), resp.content
        )
        self.assertIn(
            reverse("library_browse", args=["Math/Algebra"]).encode(), resp.content
        )

    def test_nonpdf_links_to_download(self):
        with patch(
            "judge.views.document_library.storage_listdir",
            return_value=([], ["notes.txt"]),
        ):
            resp = self.client.get(reverse("library_browse", args=["Misc"]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            reverse("library_download", args=["Misc/notes.txt"]).encode(), resp.content
        )

    def test_empty_nonexistent_folder_404(self):
        with patch(
            "judge.views.document_library.storage_listdir", return_value=([], [])
        ):
            resp = self.client.get(reverse("library_browse", args=["Ghost"]))
        self.assertEqual(resp.status_code, 404)

    def test_empty_folder_with_keep_marker_is_ok(self):
        with patch(
            "judge.views.document_library.storage_listdir", return_value=([], [".keep"])
        ):
            resp = self.client.get(reverse("library_browse", args=["EmptyButReal"]))
        self.assertEqual(resp.status_code, 200)

    def test_browse_traversal_404(self):
        resp = self.client.get("/library/browse/../../etc")
        self.assertIn(resp.status_code, (400, 404))

    def test_catalog_renders_root(self):
        with patch(
            "judge.views.document_library.storage_listdir",
            return_value=(["Math", "test"], []),
        ):
            resp = self.client.get(reverse("library_root"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Math", resp.content)

    def test_api_list_returns_json_and_hides_keep(self):
        with patch(
            "judge.views.document_library.storage_listdir",
            return_value=(["Math"], ["a.pdf", ".keep"]),
        ):
            resp = self.client.get(reverse("library_api_list") + "?path=")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual([f["name"] for f in data["folders"]], ["Math"])
        self.assertEqual([f["name"] for f in data["files"]], ["a.pdf"])

    def test_api_list_traversal_400(self):
        resp = self.client.get(reverse("library_api_list") + "?path=../../etc")
        self.assertEqual(resp.status_code, 400)

    def test_listdir_cache_and_dirty(self):
        from judge.views import document_library as dl

        with patch(
            "judge.views.document_library.storage_listdir",
            return_value=(["A"], []),
        ) as m:
            dl._cached_listdir("cachetest")  # miss -> 1 call
            dl._cached_listdir("cachetest")  # cached -> still 1
            self.assertEqual(m.call_count, 1)
            dl._dirty_listing("cachetest")
            dl._cached_listdir("cachetest")  # re-fetched -> 2
            self.assertEqual(m.call_count, 2)

    def test_download_redirects(self):
        with patch(
            "judge.views.document_library.storage_file_exists", return_value=True
        ), patch("judge.views.document_library.default_storage") as mock_storage:
            mock_storage.url.return_value = (
                "https://cdn.example/media/library/Misc/notes.txt"
            )
            resp = self.client.get(reverse("library_download", args=["Misc/notes.txt"]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("notes.txt", resp["Location"])


class LibraryManageTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3M",
            defaults={
                "name": "Python 3",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
                "template": "",
            },
        )

    def setUp(self):
        self.client = Client()
        cache.clear()
        self.admin = User.objects.create_user("lib_admin", password="pw")
        self.admin.is_superuser = True
        self.admin.save()
        Profile.objects.get_or_create(
            user=self.admin, defaults={"language": self.language}
        )
        self.normal = User.objects.create_user("lib_normal", password="pw")
        Profile.objects.get_or_create(
            user=self.normal, defaults={"language": self.language}
        )

    # --- access control ---
    def test_manage_page_route_removed(self):
        # The separate manage page is gone; editing is folded into the grid.
        resp = self.client.get("/library/manage/")
        self.assertEqual(resp.status_code, 404)

    def test_browse_edit_controls_superuser_only(self):
        with patch(
            "judge.views.document_library.storage_listdir",
            return_value=(["Math"], []),
        ):
            self.client.login(username="lib_admin", password="pw")
            resp_admin = self.client.get(reverse("library_browse", args=[""]))
            self.client.logout()
            resp_anon = self.client.get(reverse("library_browse", args=[""]))
        # Browse is public for everyone...
        self.assertEqual(resp_admin.status_code, 200)
        self.assertEqual(resp_anon.status_code, 200)
        # ...but the edit toolbar only renders for the superuser.
        self.assertIn(b"lib-new-folder", resp_admin.content)
        self.assertNotIn(b"lib-new-folder", resp_anon.content)

    def test_manage_api_forbids_non_superuser(self):
        self.client.login(username="lib_normal", password="pw")
        resp = self.client.post(
            reverse("library_manage_create_folder"), {"path": "", "name": "X"}
        )
        self.assertEqual(resp.status_code, 403)

    # --- operations (superuser) ---
    def test_create_folder(self):
        self.client.login(username="lib_admin", password="pw")
        with patch(
            "judge.views.document_library.storage_file_exists", return_value=False
        ), patch("judge.views.document_library.default_storage") as mock_storage:
            resp = self.client.post(
                reverse("library_manage_create_folder"),
                {"path": "Math", "name": "Algebra"},
            )
        self.assertEqual(resp.status_code, 200)
        saved_key = mock_storage.save.call_args[0][0]
        self.assertEqual(saved_key, "library/Math/Algebra/.keep")

    def test_upload_rejects_non_pdf(self):
        self.client.login(username="lib_admin", password="pw")
        f = SimpleUploadedFile("notes.txt", b"hi", content_type="text/plain")
        resp = self.client.post(
            reverse("library_manage_upload"), {"path": "Math", "file": f}
        )
        self.assertEqual(resp.status_code, 400)

    def test_upload_accepts_pdf(self):
        self.client.login(username="lib_admin", password="pw")
        f = SimpleUploadedFile("book.pdf", b"%PDF-1.4", content_type="application/pdf")
        with patch(
            "judge.views.document_library.storage_file_exists", return_value=False
        ), patch("judge.views.document_library.default_storage") as mock_storage:
            resp = self.client.post(
                reverse("library_manage_upload"), {"path": "Math", "file": f}
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_storage.save.call_args[0][0], "library/Math/book.pdf")

    def test_delete_nonempty_folder_blocked(self):
        self.client.login(username="lib_admin", password="pw")
        with patch(
            "judge.views.document_library.storage_listdir",
            return_value=([], ["book.pdf", ".keep"]),
        ):
            resp = self.client.post(
                reverse("library_manage_delete"), {"path": "Math", "kind": "folder"}
            )
        self.assertEqual(resp.status_code, 400)

    def test_delete_empty_folder_ok(self):
        self.client.login(username="lib_admin", password="pw")
        with patch(
            "judge.views.document_library.storage_listdir",
            return_value=([], [".keep"]),
        ), patch(
            "judge.views.document_library.storage_delete_file", return_value=True
        ) as mock_del:
            resp = self.client.post(
                reverse("library_manage_delete"), {"path": "Empty", "kind": "folder"}
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(mock_del.called)
        deleted_paths = [c.args[1] for c in mock_del.call_args_list]
        self.assertIn("library/Empty/.keep", deleted_paths)

    def test_folder_move_into_descendant_blocked(self):
        self.client.login(username="lib_admin", password="pw")
        resp = self.client.post(
            reverse("library_manage_move"),
            {"path": "Math", "dest": "Math/Algebra", "kind": "folder"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_rename_file_failure_reports_error(self):
        # storage_rename_file returning False must surface as an error, not 200.
        self.client.login(username="lib_admin", password="pw")
        with patch(
            "judge.views.document_library.storage_file_exists", return_value=False
        ), patch(
            "judge.views.document_library.storage_rename_file", return_value=False
        ):
            resp = self.client.post(
                reverse("library_manage_rename"),
                {"path": "test/a.pdf", "name": "b.pdf", "kind": "file"},
            )
        self.assertEqual(resp.status_code, 500)

    def test_folder_move_partial_failure_reports_error(self):
        # A file that fails to move must not be silently dropped.
        self.client.login(username="lib_admin", password="pw")
        with patch(
            "judge.views.document_library.storage_listdir",
            return_value=([], ["a.pdf"]),
        ), patch(
            "judge.views.document_library.storage_rename_file", return_value=False
        ):
            resp = self.client.post(
                reverse("library_manage_move"),
                {"path": "test", "dest": "Books", "kind": "folder"},
            )
        self.assertEqual(resp.status_code, 500)

    def test_delete_last_file_persists_empty_folder(self):
        # Deleting the last file must leave a .keep so the folder doesn't vanish.
        self.client.login(username="lib_admin", password="pw")
        with patch(
            "judge.views.document_library.storage_file_exists", return_value=True
        ), patch(
            "judge.views.document_library.storage_delete_file", return_value=True
        ), patch(
            "judge.views.document_library.storage_listdir", return_value=([], [])
        ), patch(
            "judge.views.document_library.default_storage"
        ) as mock_storage:
            resp = self.client.post(
                reverse("library_manage_delete"),
                {"path": "Algorithms/last.pdf", "kind": "file"},
            )
        self.assertEqual(resp.status_code, 200)
        saved = [c.args[0] for c in mock_storage.save.call_args_list]
        self.assertIn("library/Algorithms/.keep", saved)
