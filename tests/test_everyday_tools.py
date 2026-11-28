"""Everyday Life tools: email & calendar, finance, meetings, desktop.

The model is never called: ask_llm is replaced per test. Approval-gated
tools are checked both ways: declined never touches the world, approved does.
"""

import asyncio
import json
import types
from datetime import date, datetime, time as dtime, timedelta, timezone

import pytest

from modules import agent_tools, agent_tracker, email_agent, finance_agent, meeting_agent, windows_agent
from modules.everyday_common import EverydayError, ToolContext, parse_json


def run(coro):
    return asyncio.run(coro)


def ctx_with(approve, **kw):
    asked = []

    async def confirm(name, preview):
        asked.append((name, preview))
        return approve
    return ToolContext(confirm=confirm, **kw), asked


def fake_llm(*replies):
    queue = list(replies)

    async def ask(system, user, **kw):
        if not queue:
            raise EverydayError("no model")
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)
    return ask


# ─── registry ─────────────────────────────────────────────────────────

def test_registry_wires_every_tool_with_its_card_and_gate():
    names = set(agent_tools.EVERYDAY_TOOL_NAMES)
    assert names == {"triage_inbox", "auto_draft_reply", "find_free_time", "reschedule_event",
                     "track_expenses", "research_product", "add_to_wishlist",
                     "summarize_meeting", "extract_action_items", "draft_follow_up_email",
                     "get_active_window_info", "read_window_text", "click_button", "restart_crashed_game",
                     "find_local_deals", "focus_window",
                     "media_play_liked", "media_play_playlist", "media_search_and_play",
                     "media_control", "media_library", "capture_screen_context",
                     "music_set_tempo", "music_create_pattern", "music_add_chord", "music_add_melody",
                     "sing_song"}
    agents = agent_tools.EVERYDAY_TOOL_AGENT
    assert {n for n, a in agents.items() if a == "research"} == {
        "summarize_meeting", "extract_action_items", "draft_follow_up_email", "find_local_deals"}
    assert {n for n, a in agents.items() if a == "operate"} == names - {
        "summarize_meeting", "extract_action_items", "draft_follow_up_email", "find_local_deals"}
    assert all(a in agent_tracker.AGENTS for a in agents.values())
    gated = {"auto_draft_reply", "reschedule_event", "add_to_wishlist", "click_button", "restart_crashed_game",
             "find_local_deals", "capture_screen_context"}
    assert agent_tools.EVERYDAY_CONFIRM_TOOLS == gated
    assert gated <= agent_tools.CONFIRM_REQUIRED_TOOLS


def test_email_tools_are_only_offered_when_an_account_is_connected():
    hub = types.SimpleNamespace(any_connected=lambda: False)
    off = {s["function"]["name"] for s in agent_tools.everyday_tool_schemas(
        ToolContext(signals=types.SimpleNamespace(workspace_hub=hub)))}
    assert "triage_inbox" not in off and "track_expenses" in off
    hub.any_connected = lambda: True
    on = {s["function"]["name"] for s in agent_tools.everyday_tool_schemas(
        ToolContext(signals=types.SimpleNamespace(workspace_hub=hub)))}
    assert "triage_inbox" in on


def test_untrusted_results_get_provenance_markers():
    assert agent_tools.wrap_tool_result("read_window_text", "x").startswith("[FROM WINDOW]")
    assert agent_tools.wrap_tool_result("research_product", "x").startswith("[FROM WEB]")
    assert agent_tools.wrap_tool_result("triage_inbox", "x").startswith("[FROM EMAIL]")


def test_a_context_without_an_approval_path_declines():
    assert run(ToolContext().confirm("click_button", {})) is False


def test_parse_json_tolerates_fences_and_chatter():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": [1, 2]} hope that helps') == {"a": [1, 2]}
    with pytest.raises(EverydayError):
        parse_json("no json here")


# ─── email & calendar ─────────────────────────────────────────────────

class Hub:
    def __init__(self, msgs=(), events=()):
        self.msgs, self.events = list(msgs), list(events)
        self.drafts, self.moved = [], []
        self._cache = {"mail:gmail": {"data": self.msgs}}
        self.google = types.SimpleNamespace(email=lambda: "me@gmail.com")
        self.calendar = types.SimpleNamespace(
            connected=lambda: True,
            get_event=lambda eid: {"id": eid, "title": "Standup", "start": "2026-11-17T09:00:00-05:00",
                                   "attendees": [{"email": "amy@x.org"}]})

    def any_connected(self): return True
    def get_mail(self, filter="all", refresh=False): return {"messages": self.msgs, "errors": {}}
    def get_events(self, days=14): return {"events": self.events}

    def get_message(self, account, mid):
        return {"subject": "Lunch Friday?", "from": "Bob <bob@x.org>", "to": "me@gmail.com",
                "messages": [{"from": "Bob <bob@x.org>", "date": "Fri", "body": "Want to grab lunch Friday?"}]}

    def create_draft(self, account, to, subject, body, cc=None, reply_to_id=None):
        self.drafts.append((account, to, subject, body, reply_to_id))

    def reschedule_event(self, eid, new):
        self.moved.append((eid, new))
        return ({"start": "2026-11-17T09:00"}, {"title": "Standup", "start": new, "end": new})


def mail(i, subject, frm="Bob", addr="bob@x.org", unread=True, snippet=""):
    return {"id": f"t{i}", "provider": "gmail", "from_name": frm, "from_addr": addr, "subject": subject,
            "snippet": snippet, "date": "2026-11-14T09:00", "unread": unread}


def test_triage_groups_by_model_labels_and_ignores_read_mail(monkeypatch):
    hub = Hub([mail(1, "Server down, need fix today"), mail(2, "50% off sale", "Shop", "promo@shop.com"),
               mail(3, "Old thread", unread=False)])
    monkeypatch.setattr(email_agent, "ask_llm", fake_llm(
        {"items": [{"n": 1, "category": "urgent", "reason": "outage"},
                   {"n": 2, "category": "low", "reason": "promo"}]}))
    out = run(email_agent.run_tool("triage_inbox", {}, ToolContext(signals=types.SimpleNamespace(workspace_hub=hub))))
    urgent = out.split("Urgent (1):")[1].split("Important")[0]
    assert "[gmail t1]" in urgent and "outage" in urgent
    assert "[gmail t2]" in out.split("Low Priority (1):")[1]
    assert "t3" not in out


def test_triage_falls_back_to_keyword_rules_without_a_model(monkeypatch):
    hub = Hub([mail(1, "Action required: form due today"), mail(2, "Weekly digest", "News", "noreply@x.com"),
               mail(3, "Coffee next week?")])
    monkeypatch.setattr(email_agent, "ask_llm", fake_llm(EverydayError("down")))
    out = run(email_agent.triage_inbox(ToolContext(signals=types.SimpleNamespace(workspace_hub=hub))))
    assert "keyword rules" in out
    assert "t1" in out.split("Urgent")[1].split("Important")[0]
    assert "t3" in out.split("Important")[1].split("Low Priority")[0]
    assert "t2" in out.split("Low Priority")[1]


@pytest.mark.parametrize("approve", [False, True])
def test_auto_draft_reply_saves_only_when_approved(monkeypatch, approve):
    hub = Hub([mail(1, "Lunch Friday?")])
    monkeypatch.setattr(email_agent, "ask_llm", fake_llm("Sorry Bob, I can't make it Friday.\n\nMe"))
    ctx, asked = ctx_with(approve, signals=types.SimpleNamespace(workspace_hub=hub))
    out = run(email_agent.run_tool("auto_draft_reply", {"thread_id": "t1", "intent": "decline"}, ctx))
    name, preview = asked[0]
    assert name == "auto_draft_reply" and preview["to"] == "bob@x.org"
    assert preview["subject"] == "Re: Lunch Friday?" and "can't make it" in preview["body"]
    if approve:
        assert hub.drafts == [("gmail", ["bob@x.org"], "Re: Lunch Friday?", preview["body"], "t1")]
        assert "NOT sent" in out
    else:
        assert hub.drafts == [] and "declined" in out


@pytest.mark.parametrize("approve", [False, True])
def test_reschedule_asks_with_event_details(approve):
    hub = Hub()
    ctx, asked = ctx_with(approve, signals=types.SimpleNamespace(workspace_hub=hub))
    out = run(email_agent.run_tool("reschedule_event", {"event_id": "abcde123", "new_time": "2026-11-18T10:00"}, ctx))
    assert asked[0][1]["event"] == "Standup" and asked[0][1]["notifies"] == ["amy@x.org"]
    assert hub.moved == ([("abcde123", "2026-11-18T10:00")] if approve else [])
    assert ("Moved" in out) if approve else ("declined" in out)


TZ = timezone(timedelta(hours=-5))


def ev(start, end, **kw):
    return {"start": start, "end": end, "all_day": False, **kw}


def test_find_slots_skips_busy_time_and_rounds_up():
    now = datetime(2026, 11, 16, 8, 50, tzinfo=TZ)            # a Monday
    events = [ev("2026-11-16T09:00:00-05:00", "2026-11-16T10:00:00-05:00"),
              ev("2026-11-16T10:30:00-05:00", "2026-11-16T12:00:00-05:00"),
              {"start": "2026-11-16", "end": "2026-11-17", "all_day": True}]
    slots = email_agent.find_slots(events, timedelta(minutes=45), now, 3, dtime(9), dtime(17), False)
    assert slots[0] == datetime(2026, 11, 16, 12, 0, tzinfo=TZ)      # 10:00-10:30 too short
    assert slots[1].date() == date(2026, 11, 17) and slots[1].hour == 9


def test_find_slots_honours_weekends_and_day_end():
    now = datetime(2026, 11, 13, 16, 30, tzinfo=TZ)           # Friday, 30 min before day end
    slots = email_agent.find_slots([], timedelta(hours=1), now, 4, dtime(9), dtime(17), False)
    assert slots[0] == datetime(2026, 11, 16, 9, 0, tzinfo=TZ)        # skips Sat/Sun
    slots = email_agent.find_slots([], timedelta(hours=1), now, 4, dtime(9), dtime(17), True)
    assert slots[0].date() == date(2026, 11, 14)


def test_find_free_time_validates_input():
    hub = Hub()
    ctx = ToolContext(signals=types.SimpleNamespace(workspace_hub=hub))
    assert run(email_agent.find_free_time(ctx, "abc")).startswith("Error")
    assert run(email_agent.find_free_time(ctx, 30, day_start="17:00", day_end="09:00")).startswith("Error")
    assert "Next free 30-minute slot" in run(email_agent.find_free_time(ctx, 30, include_weekends=True))


# ─── finance ──────────────────────────────────────────────────────────

def test_parse_month_forms():
    today = date(2026, 3, 10)
    assert finance_agent.parse_month(None, today) == (2026, 3)
    assert finance_agent.parse_month("last month", today) == (2026, 2)
    assert finance_agent.parse_month("2025-11", today) == (2025, 11)
    assert finance_agent.parse_month("11/2025", today) == (2025, 11)
    assert finance_agent.parse_month("November", today) == (2025, 11)    # most recent November
    assert finance_agent.parse_month("feb", today) == (2026, 2)
    with pytest.raises(ValueError):
        finance_agent.parse_month("someday", today)


def test_track_expenses_reads_a_bank_export(tmp_path, monkeypatch):
    csv_path = tmp_path / "finances.csv"
    csv_path.write_text(
        "Posted Date,Description,Category,Amount\n"
        "11/02/2026,Kroger,Groceries,-54.20\n"
        "11/05/2026,Chipotle,Dining,($12.50)\n"
        "11/09/2026,Kroger,Groceries,-30.00\n"
        "11/10/2026,Paycheck,Income,1800.00\n"
        "10/30/2026,Rent,Housing,-1200.00\n"
        "garbage,row,,\n", encoding="utf-8")
    monkeypatch.setattr(finance_agent, "FINANCES_CSV", str(csv_path))
    rows = finance_agent.load_transactions(str(csv_path))
    out = finance_agent.summarize_expenses(rows, 2026, 11)
    assert "$96.70 across 3 transaction(s)" in out
    assert out.index("Groceries: $84.20") < out.index("Dining: $12.50")
    assert "Housing" not in out and "Income: $1,800.00" in out


def test_track_expenses_explains_a_missing_file(tmp_path):
    with pytest.raises(EverydayError, match="date and amount"):
        finance_agent.load_transactions(str(tmp_path / "nope.csv"))


@pytest.mark.parametrize("approve", [False, True])
def test_wishlist_writes_only_when_approved_and_dedupes(tmp_path, approve):
    path = str(tmp_path / "wishlist.json")
    ctx, asked = ctx_with(approve)
    out = run(finance_agent.add_to_wishlist(ctx, "Sony XM5", "https://shop.example/xm5", "$329.99", path=path))
    assert asked[0][1]["item"] == "Sony XM5"
    if not approve:
        assert "declined" in out and not (tmp_path / "wishlist.json").exists()
        return
    run(finance_agent.add_to_wishlist(ctx, "Sony WH-1000XM5", "https://shop.example/xm5", 299, path=path))
    items = json.loads((tmp_path / "wishlist.json").read_text(encoding="utf-8"))
    assert len(items) == 1 and items[0]["price"] == 299.0 and items[0]["name"] == "Sony WH-1000XM5"


def test_wishlist_rejects_non_web_urls():
    ctx, asked = ctx_with(True)
    assert run(finance_agent.add_to_wishlist(ctx, "x", "file:///c:/secret")).startswith("Error") and asked == []


def test_research_product_builds_a_budgeted_table(monkeypatch):
    hits = [{"href": "https://a.example/p", "title": "A", "body": "a"},
            {"href": "http://127.0.0.1/admin", "title": "local", "body": ""},
            {"href": "https://b.example/p", "title": "B", "body": "b"}]
    monkeypatch.setattr(finance_agent, "_web_search", lambda q: hits)

    async def scrape(ctx, urls):
        assert "http://127.0.0.1/admin" not in urls
        return {u: "Anker Q30 $59.99 rated 4.4 of 5. Sony XM5 $329.00. " * 20 for u in urls}
    monkeypatch.setattr(finance_agent, "_scrape", scrape)
    monkeypatch.setattr(finance_agent, "ask_llm", fake_llm(
        {"products": [{"name": "Anker Q30", "price": 59.99, "key_specs": "ANC, 40h", "rating": "4.4/5"}]},
        {"products": [{"name": "Sony XM5", "price": 329.0}, {"name": "Anker Q30", "price": 59.99}]}))
    out = run(finance_agent.research_product(ToolContext(), "headphones", 100))
    assert "| Anker Q30 | $59.99 | ANC, 40h | 4.4/5 | a.example |" in out
    assert "Over budget, left out: Sony XM5 ($329.00)" in out
    assert out.count("Anker Q30") == 1


def test_invented_prices_and_ratings_are_dropped():
    page = "Anker Q30 now $59.99. Rated 4.4 out of 5 by 1,234 buyers."
    keep = finance_agent.grounded({"name": "Q30", "price": 59.99, "rating": "4.4/5, 1,234 reviews"}, page)
    assert keep["price"] == 59.99 and keep["rating"]
    made_up = finance_agent.grounded({"name": "Q30", "price": 49.0, "rating": "4.8/5, 900 reviews",
                                      "review_notes": "comfy"}, page)
    assert made_up["price"] is None and made_up["rating"] is None and made_up["review_notes"] == "comfy"


# ─── meetings ─────────────────────────────────────────────────────────

TRANSCRIPT = "Amy: Let's ship v2 Friday.\nBob: I'll write the release notes by Thursday.\n" * 3


def test_summarize_meeting_short(monkeypatch):
    monkeypatch.setattr(meeting_agent, "ask_llm", fake_llm(
        {"summary": ["Ship v2 Friday", "Notes needed", "All agreed", "extra"],
         "action_items": [{"task": "Write release notes", "owner": "Bob", "due": "Thursday", "priority": "urgent"}]}))
    out = run(meeting_agent.summarize_meeting(TRANSCRIPT))
    assert out.count("\n- ") == 4                     # 3 bullets + 1 action item
    assert "Write release notes (owner: Bob, due: Thursday, medium priority)" in out


def test_summarize_meeting_long_transcripts_are_chunked(monkeypatch):
    calls = []

    async def ask(system, user, **kw):
        calls.append(system)
        if system == meeting_agent._CHUNK_SYSTEM:
            return json.dumps({"points": ["p"], "action_items": [{"task": "Book room"}]})
        return json.dumps({"summary": ["a", "b", "c"], "action_items": [{"task": "book room"}]})
    monkeypatch.setattr(meeting_agent, "ask_llm", ask)
    long = ("Speaker: " + "words " * 30 + "\n") * 1500
    out = run(meeting_agent.summarize_meeting(long))
    assert calls.count(meeting_agent._CHUNK_SYSTEM) == len(meeting_agent.chunk(meeting_agent.clip(long, meeting_agent.MAX_INPUT_CHARS)))
    assert calls[-1] == meeting_agent._SUMMARY_SYSTEM
    assert out.lower().count("book room") == 1         # merged and de-duplicated


def test_chunk_keeps_lines_whole():
    text = "".join(f"line {i}\n" for i in range(5000))
    parts = meeting_agent.chunk(text, 1000)
    assert "".join(parts) == text and all(p.endswith("\n") for p in parts)


def test_extract_action_items_returns_json(monkeypatch):
    monkeypatch.setattr(meeting_agent, "ask_llm", fake_llm({"action_items": ["Email Amy", {"task": "Fix bug", "owner": "Bob"}]}))
    data = json.loads(run(meeting_agent.extract_action_items("some notes")))
    assert [i["task"] for i in data["action_items"]] == ["Email Amy", "Fix bug"]


def test_follow_up_is_a_draft(monkeypatch):
    monkeypatch.setattr(meeting_agent, "ask_llm", fake_llm("Hi all, thanks for today."))
    out = run(meeting_agent.draft_follow_up_email("We agreed to ship.", [{"task": "x"}]))
    assert out.startswith("Subject: Meeting follow-up") and "not sent" in out


# ─── desktop ──────────────────────────────────────────────────────────

def test_lumis_own_windows_and_vaults_are_off_limits():
    assert windows_agent.blocked_reason("Lumi Island", "lumi-island.exe", 1)
    assert windows_agent.blocked_reason("Vault", "1Password.exe", 1)
    assert windows_agent.blocked_reason("Anything", "python.exe", windows_agent.os.getpid())
    assert windows_agent.blocked_reason("Untitled - Notepad", "notepad.exe", 1) is None


@pytest.mark.parametrize("approve", [False, True])
def test_click_button_only_clicks_after_approval(monkeypatch, approve):
    clicked = []
    target = {"action": "Click button", "button": "Save", "window": "Untitled - Notepad", "app": "notepad.exe"}
    monkeypatch.setattr(windows_agent, "_describe_target", lambda w, b: dict(target))
    monkeypatch.setattr(windows_agent, "_do_click", lambda w, b, t: clicked.append(t) or "Clicked")
    ctx, asked = ctx_with(approve)
    run(windows_agent.click_button(ctx, "Notepad", "Save"))
    assert asked == [("click_button", target)]
    assert clicked == ([target] if approve else [])


# ─── prompter dispatch ────────────────────────────────────────────────

def make_prompter(approve, hub=None):
    from modules.prompter import Prompter
    p = Prompter.__new__(Prompter)
    p.signals = types.SimpleNamespace(workspace_hub=hub, sdk=None)
    p.modules = []
    p.asked = []

    async def confirm(name, arguments, timeout=120, agent="lead"):
        p.asked.append((name, agent))
        return approve
    p._confirm = confirm
    return p


def test_prompter_runs_system_tools_on_the_system_card(tmp_path, monkeypatch):
    seen = []
    agent_tracker.register(cb := lambda snap: seen.append(snap["operate"]["status"]))
    try:
        p = make_prompter(False, hub=Hub())
        out = run(p._dispatch_tool_call("reschedule_event", {"event_id": "abcde123", "new_time": "2026-11-18T10:00"}))
    finally:
        agent_tracker.unregister(cb)
    assert "declined" in out and p.asked == [("reschedule_event", "operate")]
    assert "executing" in seen and agent_tracker.get("operate")["status"] == "idle"


def test_prompter_runs_meeting_tools_on_research(monkeypatch):
    monkeypatch.setattr(meeting_agent, "ask_llm", fake_llm({"action_items": []}))
    p = make_prompter(True)
    out = run(p._dispatch_tool_call("extract_action_items", {"text": "nothing to do"}))
    assert json.loads(out) == {"action_items": []} and p.asked == []
    assert agent_tracker.get("research")["status"] == "idle"          # the meeting work is done
    assert agent_tracker.get("lead")["status"] == "thinking"           # and Lead reads the result
    agent_tracker.update("lead", "idle")


def test_tool_failures_put_the_system_card_in_error():
    p = make_prompter(True, hub=types.SimpleNamespace(any_connected=lambda: False))
    out = run(p._dispatch_tool_call("triage_inbox", {}))
    assert "aren't connected" in out
