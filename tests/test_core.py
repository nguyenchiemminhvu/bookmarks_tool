import json
import tempfile
import time
import unittest
from pathlib import Path

from bookmarks.backup import BACKUP_INTERVAL, PREFIX, BackupScheduler, is_due, list_backups, write_backup
from bookmarks.credentials import CredentialStore
from bookmarks.errors import DuplicateURL, NotFound
from bookmarks.importer import parse_bookmarks_html
from bookmarks.store import Store
from bookmarks.tags import normalize_tag, normalize_tags

EXPORT = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p>
    <DT><H3 PERSONAL_TOOLBAR_FOLDER="true">Bookmarks bar</H3>
    <DL><p>
        <DT><H3>BMW ICON 25</H3>
        <DL><p>
            <DT><H3>Collab</H3>
            <DL><p>
                <DT><A HREF="http://collab.example.com/a" ADD_DATE="1700000000">Page &amp; A</A>
            </DL><p>
            <DT><A HREF="http://jira.example.com/x">Board</A>
        </DL><p>
        <DT><H3>Programming</H3>
        <DL><p>
            <DT><H3>C/C++</H3>
            <DL><p>
                <DT><A HREF="http://collab.example.com/a">Same page again</A>
            </DL><p>
        </DL><p>
        <DT><A HREF="https://top.example.com/">Top level</A>
    </DL><p>
</DL><p>
"""


class TagTests(unittest.TestCase):
    def test_normalize_folder_names(self):
        cases = {
            "C/C++": "c-cpp",
            "Data Structures & Algorithms": "data-structures-and-algorithms",
            "BMW ICON 25": "bmw-icon-25",
            "TCP/IP (Internet Protocol suite)": "tcp-ip-internet-protocol-suite",
            "C#": "csharp",
            "Đà Nẵng": "da-nang",
            "  --.. ": "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_tag(raw), expected)

    def test_normalize_tags_dedupes_and_drops_empty(self):
        self.assertEqual(normalize_tags(["Jira", "jira", "", "Collab"]), ["collab", "jira"])


class ImporterTests(unittest.TestCase):
    def test_folder_path_becomes_tags_without_root(self):
        links = parse_bookmarks_html(EXPORT)
        self.assertEqual([(l.url, l.folders) for l in links], [
            ("http://collab.example.com/a", ["BMW ICON 25", "Collab"]),
            ("http://jira.example.com/x", ["BMW ICON 25"]),
            ("http://collab.example.com/a", ["Programming", "C/C++"]),
            ("https://top.example.com/", []),
        ])
        self.assertEqual(links[0].title, "Page & A")
        self.assertEqual(links[0].add_date, 1700000000)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.db")

    def tearDown(self):
        self.tmp.cleanup()

    def tags_of(self, url):
        return next(l["tags"] for l in self.store.list_links() if l["url"] == url)

    def test_import_merges_duplicate_urls(self):
        result = self.store.import_links(parse_bookmarks_html(EXPORT))
        self.assertEqual(result, {"added": 3, "merged": 1, "skipped": 0})
        self.assertEqual(self.tags_of("http://collab.example.com/a"),
                         ["bmw-icon-25", "c-cpp", "collab", "programming"])
        self.assertEqual(self.tags_of("https://top.example.com/"), [])

    def test_duplicate_url_rejected_on_add_and_update(self):
        self.store.add_link("https://a.example.com/")
        other = self.store.add_link("b.example.com")  # scheme added
        self.assertEqual(other["url"], "https://b.example.com")
        with self.assertRaises(DuplicateURL):
            self.store.add_link("https://a.example.com/")
        with self.assertRaises(DuplicateURL):
            self.store.update_link(other["id"], "https://a.example.com/")

    def test_url_change_clears_check_result(self):
        link = self.store.add_link("https://a.example.com/", tags=["x"])
        self.store.set_check_result(link["id"], "broken", 404, None, 1)
        same = self.store.update_link(link["id"], link["url"], title="t", tags=["x"])
        self.assertEqual(same["check_status"], "broken")
        moved = self.store.update_link(link["id"], "https://new.example.com/", tags=["x"])
        self.assertIsNone(moved["check_status"])

    def test_rename_into_existing_tag_merges(self):
        a = self.store.add_link("https://a.example.com/", tags=["jira", "bmw"])
        b = self.store.add_link("https://b.example.com/", tags=["JIRA-old"])
        self.assertEqual(self.store.rename_tag("jira-old", "Jira"), "jira")
        self.assertEqual(self.store.get_link(a["id"])["tags"], ["bmw", "jira"])
        self.assertEqual(self.store.get_link(b["id"])["tags"], ["jira"])
        with self.assertRaises(NotFound):
            self.store.rename_tag("jira-old", "x")

    def test_delete_tag_keeps_links_and_unused_tags_are_pruned(self):
        a = self.store.add_link("https://a.example.com/", tags=["temp", "keep"])
        self.store.delete_tag("temp")
        self.assertEqual(self.store.get_link(a["id"])["tags"], ["keep"])
        self.store.retag([a["id"], 999], add=["new"], remove=["keep"])
        self.assertEqual(self.store.get_link(a["id"])["tags"], ["new"])
        self.store.delete_link(a["id"])
        with self.assertRaises(NotFound):
            self.store.rename_tag("new", "x")  # tag vanished with its last link


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "b.db")
        self.store.add_link("https://a.example.com/", title="A", notes="n", tags=["x", "y"])
        self.store.set_check_result(1, "broken", 404, None, 5)

    def tearDown(self):
        self.tmp.cleanup()

    def test_backup_round_trips_into_a_fresh_store(self):
        path = write_backup(self.store, self.dir)
        fresh = Store(self.dir / "fresh.db")
        result = fresh.import_backup(json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(result, {"added": 1, "merged": 0, "skipped": 0})
        link = fresh.list_links()[0]
        self.assertEqual((link["title"], link["notes"], link["tags"], link["check_status"]),
                         ("A", "n", ["x", "y"], "broken"))
        with self.assertRaises(ValueError):
            fresh.import_backup({"nope": []})

    def test_due_only_when_newest_backup_is_old_and_old_ones_are_pruned(self):
        self.assertTrue(is_due(self.dir, time.time()))
        scheduler = BackupScheduler(self.store, self.dir)
        self.assertIsNotNone(scheduler.run_once())
        self.assertIsNone(scheduler.run_once())
        self.assertTrue(is_due(self.dir, time.time() + BACKUP_INTERVAL + 1))
        for day in range(1, 11):
            (self.dir / f"{PREFIX}2000010{day % 10}-{day:06d}.json").write_text("{}")
        write_backup(self.store, self.dir, keep=3)
        self.assertEqual(len(list_backups(self.dir)), 3)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "credentials.json"
        self.creds = CredentialStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_most_specific_host_wins_and_secret_not_exposed(self):
        wide = self.creds.add({"host": "*.example.com", "type": "bearer", "secret": "wide"})
        self.creds.add({"host": "https://git.example.com/group/repo", "type": "header",
                        "header": "PRIVATE-TOKEN", "secret": "gl"})
        self.assertNotIn("secret", wide)
        self.assertEqual(self.creds.headers_for("https://jira.example.com/browse/X-1"),
                         {"Authorization": "Bearer wide"})
        self.assertEqual(self.creds.headers_for("https://git.example.com/x"), {"PRIVATE-TOKEN": "gl"})
        self.assertEqual(self.creds.headers_for("https://example.com/"), {})
        self.assertEqual(self.creds.headers_for("https://evil-example.com/"), {})
        self.assertTrue(all("secret" not in c for c in self.creds.list()))

    def test_update_without_secret_keeps_it_and_persists(self):
        cred = self.creds.add({"host": "jira.example.com", "type": "basic", "username": "me", "secret": "pw"})
        self.creds.update(cred["id"], {"host": "jira.example.com", "type": "basic", "username": "me2"})
        reloaded = CredentialStore(self.path)
        self.assertEqual(reloaded.headers_for("https://jira.example.com/"),
                         {"Authorization": "Basic bWUyOnB3"})  # me2:pw
        self.assertEqual(json.loads(self.path.read_text())["credentials"][0]["secret"], "pw")

    def test_rejects_header_injection_and_bad_input(self):
        bad = [
            {"host": "a.example.com", "type": "bearer", "secret": "x\r\nX-Evil: 1"},
            {"host": "a.example.com", "type": "header", "header": "Bad Header", "secret": "x"},
            {"host": "a.example.com", "type": "basic", "username": "a:b", "secret": "x"},
            {"host": "not a host", "type": "bearer", "secret": "x"},
            {"host": "a.example.com", "type": "bearer"},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.creds.add(data)
        self.creds.add({"host": "a.example.com", "type": "bearer", "secret": "x"})
        with self.assertRaises(ValueError):
            self.creds.add({"host": "a.example.com", "type": "bearer", "secret": "y"})


if __name__ == "__main__":
    unittest.main()
