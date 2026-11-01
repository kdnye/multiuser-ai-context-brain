"""Tests for mcp_server.store (file store, cached store, Cloud Storage backend) and the wiki running on them."""

from __future__ import annotations

import threading

import pytest

from mcp_server.store import CachedStore, FileStore, GcsBackend
from mcp_server.wiki_store import Wiki, WikiError

PAGE = '---\ntitle: "{t}"\ntype: "note"\nscope: "work"\nsource_ids: ["x"]\nupdated: "2026-01-01"\n---\n# {t}\n\n{body}\n'


class FakeBackend:
    def __init__(self, pages=None):
        self.pages = dict(pages or {})
        self.calls: list[tuple] = []

    def list_names(self):
        self.calls.append(("list",))
        return sorted(self.pages)

    def get(self, name):
        self.calls.append(("get", name))
        return self.pages.get(name)

    def put(self, name, text):
        self.calls.append(("put", name))
        self.pages[name] = text


# --- FileStore --------------------------------------------------------------------------------

def test_file_store_reads_writes_lists_and_refuses_escapes(tmp_path):
    s = FileStore(tmp_path / "w")
    assert s.names() == [] and s.read("notes/a.md") is None
    s.write("notes/a.md", "hello")
    s.write("index.md", "idx")
    assert s.names() == ["index.md", "notes/a.md"] and s.read("notes/a.md") == "hello"
    for bad in ("../x.md", "/abs.md", "notes/a.txt", "notes/../../x.md"):
        with pytest.raises(ValueError):
            s.read(bad)
    assert not (tmp_path / "x.md").exists()


# --- CachedStore ------------------------------------------------------------------------------

def test_cached_store_loads_once_then_serves_from_memory():
    b = FakeBackend({"notes/a.md": "A", "notes/b.md": "B"})
    s = CachedStore(b)
    assert s.warm() == 2
    assert s.names() == ["notes/a.md", "notes/b.md"] and s.read("notes/a.md") == "A" and s.read("nope.md") is None
    gets_after_warm = sum(1 for c in b.calls if c[0] == "get")
    for _ in range(5):
        s.read("notes/a.md")
        s.names()
    assert sum(1 for c in b.calls if c[0] == "get") == gets_after_warm == 2   # no further backend reads
    assert sum(1 for c in b.calls if c[0] == "list") == 1


def test_cached_store_writes_through_backend_first():
    b = FakeBackend({"notes/a.md": "A"})
    s = CachedStore(b)
    s.write("notes/new.md", "N")
    assert b.pages["notes/new.md"] == "N" and s.read("notes/new.md") == "N" and "notes/new.md" in s.names()

    class Failing(FakeBackend):
        def put(self, name, text):
            raise OSError("bucket down")

    f = CachedStore(Failing({"notes/a.md": "A"}))
    with pytest.raises(OSError):
        f.write("notes/a.md", "CHANGED")
    assert f.read("notes/a.md") == "A"            # memory never runs ahead of the durable copy


def test_cached_store_survives_concurrent_writers():
    b = FakeBackend()
    s = CachedStore(b)
    threads = [threading.Thread(target=lambda i=i: s.write(f"notes/p{i}.md", str(i))) for i in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(s.names()) == 40 and len(b.pages) == 40


# --- GcsBackend (fake bucket) -----------------------------------------------------------------

def test_token_credentials_come_from_the_env_only_when_set(monkeypatch):
    from mcp_server.store import _token_credentials

    monkeypatch.delenv("BRAIN_GCS_TOKEN", raising=False)
    assert _token_credentials() is None                      # Cloud Run / mirror: fall back to the environment
    monkeypatch.setenv("BRAIN_GCS_TOKEN", "ya29.test")
    assert _token_credentials().token == "ya29.test"


class NotFound(Exception):
    pass


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def download_as_text(self, encoding="utf-8"):
        if self.name not in self.bucket.objects:
            raise NotFound(self.name)
        return self.bucket.objects[self.name].decode(encoding)

    def upload_from_string(self, data, content_type=None):
        self.bucket.objects[self.name] = data
        self.bucket.content_types[self.name] = content_type


class FakeBucket:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}

    def blob(self, name):
        return FakeBlob(self, name)


class FakeClient:
    def __init__(self):
        self.b = FakeBucket()

    def bucket(self, name):
        assert name == "my-bucket"
        return self.b

    def list_blobs(self, bucket, prefix=""):
        return [FakeBlob(bucket, n) for n in sorted(bucket.objects) if n.startswith(prefix)]


def test_gcs_backend_uses_the_prefix_and_maps_not_found_to_none():
    client = FakeClient()
    g = GcsBackend("my-bucket", prefix="wiki", client=client)
    g.put("notes/a.md", "héllo")
    client.b.objects["other/x.md"] = b"unrelated"
    client.b.objects["wiki/notes/readme.txt"] = b"not a page"
    assert "wiki/notes/a.md" in client.b.objects and client.b.content_types["wiki/notes/a.md"].startswith("text/markdown")
    assert g.list_names() == ["notes/a.md"]            # prefix stripped; foreign objects and non-.md ignored
    assert g.get("notes/a.md") == "héllo" and g.get("notes/missing.md") is None


def test_gcs_backend_does_not_swallow_other_errors():
    class Boom(FakeBlob):
        def download_as_text(self, encoding="utf-8"):
            raise PermissionError("denied")

    client = FakeClient()
    client.b.blob = lambda name: Boom(client.b, name)
    with pytest.raises(PermissionError):
        GcsBackend("my-bucket", client=client).get("notes/a.md")


# --- the wiki running on the cached cloud-style store ----------------------------------------

def test_wiki_works_end_to_end_on_a_cached_store():
    client = FakeClient()
    backend = GcsBackend("my-bucket", prefix="wiki", client=client)
    backend.put("projects/widgets.md", PAGE.format(t="WIDGETS", body="Widget ordering tool.").replace('"note"', '"project"'))
    w = Wiki(CachedStore(backend), today=lambda: "2026-10-27")
    assert [p["path"] for p in w.list_pages()] == ["projects/widgets"]
    r = w.write("notes/mail", PAGE.format(t="Mail rules", body="Ask pat.smith@example.com. See [[projects/widgets]]."), "capture mail rules")
    assert r["action"] == "created" and r["scrubbed"] == {"email": 1} and r["warnings"] == []
    assert "wiki/notes/mail.md" in client.b.objects and "wiki/index.md" in client.b.objects and "wiki/log.md" in client.b.objects   # durable copy
    assert b"{email}" in client.b.objects["wiki/notes/mail.md"] and b"pat.smith@example.com" not in client.b.objects["wiki/notes/mail.md"]
    assert w.search("mail rules")[0]["path"] == "notes/mail"
    assert w.log_tail(1)[0].startswith("## [2026-10-27] create | notes/mail")
    ing = w.ingest_session({"session": {"title": "Daily chat on 2026-10-27 concerning WIDGETS", "date": "2026-10-27", "summary": "Worked.", "concerning": "WIDGETS"},
                            "notes": [{"title": "Fee rules", "project": "WIDGETS", "body": "Body."}]})
    assert ing["session"] == "sessions/2026-10-27-widgets" and "[[projects/widgets|WIDGETS]]" in w.read(ing["session"])
    assert w.lint()["by_kind"].get("broken_link") is None
    # a fresh process (new cache) sees exactly what was written: the bucket is the source of truth
    w2 = Wiki(CachedStore(GcsBackend("my-bucket", prefix="wiki", client=client)), today=lambda: "2026-10-28")
    assert w2.read("notes/mail") == w.read("notes/mail") and len(w2.list_pages()) == len(w.list_pages())


def test_wiki_over_a_store_still_refuses_bad_paths():
    w = Wiki(CachedStore(FakeBackend()))
    for bad in ("../x", "notes/../a", "random/a", "notes/A"):
        with pytest.raises(WikiError):
            w.read(bad)
