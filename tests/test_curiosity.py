import random

from modules import curiosity as cu


def test_only_when_idle_long_enough_and_not_gaming():
    assert cu.should_run(idle_s=601, gaming=False) is True
    assert cu.should_run(idle_s=599, gaming=False) is False
    assert cu.should_run(idle_s=9999, gaming=True) is False


def test_only_https_to_public_hosts():
    assert cu.safe_url("https://example.com/page") is True
    assert cu.safe_url("http://example.com") is False
    assert cu.safe_url("https://localhost/x") is False
    assert cu.safe_url("https://192.168.1.10/") is False
    assert cu.safe_url("https://127.0.0.1/") is False
    assert cu.safe_url("https://printer.local/") is False
    assert cu.safe_url("not a url") is False


def test_topics_come_from_the_pool_and_an_empty_pool_gives_none():
    assert cu.pick_topic(["robotics"], random.Random(1)) == "robotics"
    assert cu.pick_topic([], random.Random(1)) is None


def test_a_pass_keeps_only_safe_results_and_caps_the_count(tmp_path):
    store = cu.CuriosityStore(str(tmp_path / "c.db"))
    hits = [{"url": "https://a.example/1", "snippet": "one"},
            {"url": "http://bad.example/", "snippet": "not https"},
            {"url": "https://b.example/2", "snippet": "two"},
            {"url": "https://c.example/3", "snippet": "three"}]
    kept = cu.run_once(store, ["robotics"], lambda q: hits, now=100.0, rng=random.Random(0))
    assert kept == cu.MAX_NOTES_PER_RUN
    rows = store.unsurfaced()
    assert all(r["url"].startswith("https://") for r in rows) and len(rows) == 2


def test_discoveries_are_tagged_and_unsurfaced_until_mentioned(tmp_path):
    store = cu.CuriosityStore(str(tmp_path / "c.db"))
    store.add("chess", "a note", "https://x.example", 5.0)
    rows = store.unsurfaced()
    assert len(rows) == 1
    store.mark_surfaced([rows[0]["id"]])
    assert store.unsurfaced() == []
    raw = store._conn.execute("SELECT source FROM discoveries").fetchone()[0]
    assert raw == "idle_curiosity"
