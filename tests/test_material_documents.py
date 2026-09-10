"""Ultra BBML files, legacy attachments, partial results and safe downloads."""

import json
import unittest
from html import escape
from unittest.mock import MagicMock, Mock, patch

import requests
import test_hardening as base

from zoom2txt.blackboard import client as bc
from zoom2txt.blackboard.materials import document_files, material_url
from zoom2txt.gui import workers
from zoom2txt.storage import StateStore, atomic_write, digest

BASE = "https://campus.example.invalid"


def bbml(xid="100_1", name="Semana 01.pdf", query="sig=TEST_SIGNATURE", render="attachment"):
    metadata = escape(
        json.dumps({"render": render, "linkName": name, "mimeType": "application/pdf"}),
        quote=True,
    )
    return f'<a href="{BASE}/bbcswebdav/pid-10-dt-content-rid-{xid}/xid-{xid}?{query}" data-bbfile="{metadata}">{name}</a>'


def item(
    identifier="doc",
    body=None,
    handler="resource/x-bb-document",
    title="ultraDocumentBody",
    parent="folder",
):
    row = dict(
        id=identifier,
        parentId=parent,
        title=title,
        contentHandler={"id": handler},
        modified="2026-09-04T12:00:00Z",
    )
    if body is not None:
        row["body"] = body
    return row


class MaterialDocumentTests(unittest.TestCase):
    setUp = base.HardeningTests.setUp
    tearDown = base.HardeningTests.tearDown
    client = base.HardeningTests.client

    def materials_client(self, items):
        client = self.client()
        client.base = BASE
        client._all_contents = Mock(return_value=items)
        return client

    def test_ultra_does_not_request_unsupported_attachments_endpoint(self):
        client = self.materials_client(
            [
                item(
                    "folder",
                    "",
                    handler="resource/x-bb-folder",
                    title="Semana 1",
                    parent=None,
                ),
                item(body=bbml()),
            ]
        )
        files = client.list_course_materials("c", "Course")
        client.session.get.assert_not_called()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].attachment_id, "xid-100_1")
        self.assertTrue(files[0].rel_path.startswith("Semana 1/"))
        self.assertTrue(files[0].filename.endswith(".pdf"))
        self.assertNotIn("TEST_SIGNATURE", repr(files[0]))

    def test_changed_signed_link_keeps_same_identity_path_and_version(self):
        client = self.materials_client([item(body=bbml(query="signature=FIRST"))])
        old = client.list_course_materials("c", "Course")[0]
        client._all_contents.return_value = [item(body=bbml(query="signature=SECOND"))]
        new = client.list_course_materials("c", "Course")[0]
        self.assertEqual(
            (old.attachment_id, old.rel_path, old.version),
            (new.attachment_id, new.rel_path, new.version),
        )
        self.assertNotEqual(old.download_url, new.download_url)

    def test_learning_module_is_container_not_unexportable_file(self):
        client = self.materials_client(
            [
                item(
                    "module",
                    handler="resource/x-bb-lesson",
                    title="Unidad 01",
                    parent=None,
                ),
                item(body=bbml(), parent="module"),
            ]
        )
        files = client.list_course_materials("c", "Course")
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].rel_path.startswith("Unidad 01/"))
        self.assertEqual(client.coverage, [])
        self.assertEqual(client.material_issues, [])

    def test_inline_files_and_duplicates(self):
        parsed = document_files(
            BASE,
            bbml(render="inline")
            + bbml()
            + f'<img src="{BASE}/bbcswebdav/xid-200_1" alt="Redes.png"/>',
        )
        self.assertEqual([f.id for f in parsed.files], ["xid-100_1", "xid-200_1"])
        self.assertEqual(parsed.files[1].filename, "Redes.png")

    def test_relative_url_and_entities(self):
        body = '<a href="/bbcswebdav/xid-123_1?a=1&amp;b=2">Introducción &amp; práctica.pdf</a>'
        parsed = document_files(BASE, body)
        self.assertEqual(parsed.files[0].filename, "Introducción & práctica.pdf")
        self.assertEqual(parsed.files[0].url, BASE + "/bbcswebdav/xid-123_1?a=1&b=2")

    def test_external_resources_and_scripts_are_not_downloaded(self):
        parsed = document_files(
            BASE,
            '<script><a href="/bbcswebdav/xid-1_1">Bad.pdf</a></script><a href="https://example.org/video">Video externo</a>'
            + bbml(),
        )
        self.assertEqual(len(parsed.files), 1)
        self.assertEqual(parsed.external_links, 1)

    def test_unknown_or_foreign_file_is_reported_not_silently_ignored(self):
        for body in (
            '<a href="/bbcswebdav/unknown">Documento</a>',
            bbml().replace(BASE, "https://evil.invalid"),
        ):
            parsed = document_files(BASE, body)
            self.assertEqual(parsed.files, [])
            self.assertTrue(parsed.issues)
            self.assertNotIn("TEST_SIGNATURE", str(parsed.issues))

    def test_material_url_rejects_credentials_cross_origin_and_traversal(self):
        for value in (
            "http://campus.example.invalid/bbcswebdav/xid-1_1",
            "https://campus.example.invalid.evil.invalid/bbcswebdav/xid-1_1",
            "https://user:secret@campus.example.invalid/bbcswebdav/xid-1_1",
            "/bbcswebdav/%2e%2e/webapps/logout",
            "/webapps/logout",
            "file:///C:/private",
        ):
            with self.assertRaises(ValueError):
                material_url(BASE, value)

    def test_missing_document_body_is_fetched_before_parsing(self):
        client = self.materials_client([item()])
        client.session.get.return_value = Mock(status_code=200, json=lambda: {"body": bbml()})
        self.assertEqual(len(client.list_course_materials("c", "Course")), 1)
        self.assertTrue(client.session.get.call_args.args[0].endswith("/contents/doc"))

    def test_named_ultra_document_uses_body_based_on_course_mode(self):
        client = self.materials_client([item(title="Lectura complementaria", body=bbml())])
        client._course_modes = {"c": "Ultra"}
        self.assertEqual(len(client.list_course_materials("c", "Course")), 1)
        client.session.get.assert_not_called()

    def test_empty_and_text_only_document_are_not_http_errors(self):
        client = self.materials_client(
            [item(body="<p>Indicaciones de la actividad.</p>"), item("empty", body="")]
        )
        self.assertEqual(client.list_course_materials("c", "Course"), [])
        self.assertEqual(client.material_issues, [])
        self.assertTrue(client.coverage)
        client.session.get.assert_not_called()

    def test_classic_document_and_file_keep_attachment_endpoint(self):
        for handler in ("resource/x-bb-document", "resource/x-bb-file"):
            client = self.materials_client([item(handler=handler, title="Lecture", body="")])
            client.session.get.return_value = Mock(
                status_code=200,
                json=lambda: {"results": [{"id": "a", "fileName": "Lecture.pdf"}]},
            )
            files = client.list_course_materials("c", "Course")
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].download_url, "")
            self.assertTrue(client.session.get.call_args.args[0].endswith("/attachments"))

    def test_partial_resource_failure_preserves_other_files(self):
        client = self.materials_client([item("broken"), item("good", body=bbml())])
        client.session.get.return_value = Mock(status_code=403)
        files = client.list_course_materials("c", "Course")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].content_id, "good")
        self.assertEqual(len(client.material_issues), 1)

    def test_attachment_error_is_visible_and_other_resources_continue(self):
        client = self.materials_client(
            [
                item("bad", handler="resource/x-bb-file", title="Bad.pdf"),
                item("good", body=bbml()),
            ]
        )
        client.session.get.return_value = Mock(status_code=400)
        self.assertEqual(len(client.list_course_materials("c", "Course")), 1)
        self.assertIn("400", client.material_issues[0])

    def test_duplicate_filenames_remain_distinct(self):
        client = self.materials_client([item(body=bbml() + bbml("200_1"))])
        files = client.list_course_materials("c", "Course")
        self.assertEqual(len({f.rel_path for f in files}), 2)

    def test_embedded_download_uses_discovered_url_and_verifies_bytes(self):
        client = self.materials_client([item(body=bbml())])
        mf = client.list_course_materials("c", "Course")[0]
        content = b"%PDF-1.7\nfixture"
        response = Mock(
            status_code=200,
            headers={"Content-Length": str(len(content))},
            url=mf.download_url,
        )
        response.iter_content.return_value = iter([content])
        client.session.get.return_value = MagicMock()
        client.session.get.return_value.__enter__.return_value = response
        dest = self.root / "download.pdf"
        self.assertEqual(client.download_attachment(mf, dest), len(content))
        self.assertEqual(dest.read_bytes(), content)
        self.assertEqual(client.session.get.call_args.args[0], mf.download_url)

    def test_expired_link_keeps_previous_file(self):
        client = self.materials_client([item(body=bbml())])
        mf = client.list_course_materials("c", "Course")[0]
        client.session.get.return_value = MagicMock()
        client.session.get.return_value.__enter__.return_value = Mock(status_code=403)
        dest = self.root / "download.pdf"
        atomic_write(dest, b"%PDF-old")
        with self.assertRaisesRegex(bc.BlackboardError, "Buscar material"):
            client.download_attachment(mf, dest)
        self.assertEqual(dest.read_bytes(), b"%PDF-old")

    def test_network_exception_does_not_leak_signed_query(self):
        client = self.materials_client([item(body=bbml())])
        mf = client.list_course_materials("c", "Course")[0]
        client.session.get.side_effect = requests.ConnectionError(mf.download_url)
        with self.assertRaises(bc.BlackboardError) as exc:
            client.download_attachment(mf, self.root / "download.pdf")
        self.assertNotIn("TEST_SIGNATURE", str(exc.exception))

    def test_worker_offers_files_from_partial_course_and_recognizes_saved_file(self):
        client = self.materials_client([item("bad"), item("good", body=bbml())])
        client.session.get.return_value = Mock(status_code=403)
        client.is_logged_in = lambda: True
        client.get_current_courses = lambda: [{"id": "c", "name": "Course"}]
        results = []
        with patch.object(bc, "BlackboardClient", return_value=client):
            worker = workers.MaterialScanWorker(self.s)
            worker.found.connect(results.append)
            worker.run()
            rows, missing = results[-1]
            self.assertTrue(rows[0]["error"])
            self.assertEqual(rows[0]["total"], 1)
            self.assertEqual(len(missing), 1)
            pending = missing[0]
            atomic_write(pending["dest"], b"%PDF-fixture")
            StateStore(self.s).put(
                "material",
                pending["key"],
                version=pending["mf"].version,
                checksum=digest(pending["dest"]),
            )
            worker.run()
        self.assertEqual(results[-1][0][0]["have"], 1)
        self.assertEqual(results[-1][1], [])
        self.assertNotIn("TEST_SIGNATURE", json.dumps(StateStore(self.s).list("material")))


if __name__ == "__main__":
    unittest.main()
