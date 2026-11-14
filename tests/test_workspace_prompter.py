"""The Prompter's dispatch of workspace tools: outbound ones stop at the
approval prompt and never reach the provider when declined."""

import asyncio
import types

import modules.workspace_hub as wh
from modules.prompter import Prompter
from modules.workspace_hub import WorkspaceHub


class Mail:
    can_send = True

    def __init__(self): self.sent = []
    def connected(self): return True
    def list_recent(self, limit): return []
    def send(self, to, cc, subject, body, reply_to=None): self.sent.append((to, subject, body))


class Cal:
    def __init__(self): self.deleted = []
    def connected(self): return True
    def list_events(self, days=30, limit=100): return []
    def get_event(self, eid):
        return {"id": eid, "title": "Dentist", "start": "2026-11-17T15:00:00-05:00"}
    def delete_event(self, eid): self.deleted.append(eid)


def make(tmp_path, approve):
    hub = WorkspaceHub(cred_dir=str(tmp_path / "c"), cache_path=str(tmp_path / "cache.json"))
    hub.calendar, mail = Cal(), Mail()
    hub._providers = {"gmail": mail}
    p = Prompter.__new__(Prompter)
    p.signals = types.SimpleNamespace(workspace_hub=hub, sdk=None)
    p.modules = []
    asked, agents = [], []

    async def confirm(name, arguments, timeout=120, agent="main"):
        asked.append((name, arguments))
        agents.append(agent)
        return approve
    p._confirm = confirm
    p._agents_asked = agents
    return p, hub, mail, asked


def dispatch(p, name, args):
    return asyncio.run(p._dispatch_tool_call(name, args))


def test_declined_send_never_reaches_the_provider(tmp_path):
    p, hub, mail, asked = make(tmp_path, approve=False)
    out = dispatch(p, "send_email", {"account": "gmail", "to": ["bob@x.org"], "subject": "Hi", "body": "Dear Bob"})
    assert "declined" in out and mail.sent == []
    name, preview = asked[0]
    assert name == "send_email" and preview["to"] == ["bob@x.org"] and preview["body"] == "Dear Bob"


def test_approved_send_goes_out(tmp_path):
    p, hub, mail, asked = make(tmp_path, approve=True)
    assert dispatch(p, "send_email", {"account": "gmail", "to": ["bob@x.org"], "subject": "Hi", "body": "Hello"}) == "Email sent."
    assert mail.sent == [(["bob@x.org"], "Hi", "Hello")]


def test_delete_event_prompt_names_the_event(tmp_path):
    p, hub, mail, asked = make(tmp_path, approve=False)
    assert "declined" in dispatch(p, "delete_event", {"event_id": "abcde12345"})
    assert "Dentist" in asked[0][1]["event"] and hub.calendar.deleted == []
    p, hub, mail, asked = make(tmp_path, approve=True)
    dispatch(p, "delete_event", {"event_id": "abcde12345"})
    assert hub.calendar.deleted == ["abcde12345"]


def test_read_only_tools_do_not_prompt(tmp_path):
    p, hub, mail, asked = make(tmp_path, approve=False)
    assert dispatch(p, "list_events", {"days": 3}).startswith("No events")
    assert dispatch(p, "list_emails", {}) == "No messages."
    assert asked == []


def test_tools_refuse_politely_when_nothing_is_connected(tmp_path):
    p, hub, mail, asked = make(tmp_path, approve=True)
    hub.calendar = types.SimpleNamespace(connected=lambda: False)
    hub._providers = {}
    out = dispatch(p, "list_emails", {})
    assert "aren't connected" in out and asked == []


def test_calendar_and_email_approvals_wait_on_the_office_card(tmp_path):
    p, hub, mail, asked = make(tmp_path, approve=False)
    dispatch(p, "send_email", {"account": "gmail", "to": ["bob@x.org"], "subject": "Hi", "body": "x"})
    dispatch(p, "delete_event", {"event_id": "abcde12345"})
    assert p._agents_asked == ["office", "office"]
