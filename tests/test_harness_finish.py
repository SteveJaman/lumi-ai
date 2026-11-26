"""Finishing with task_complete, not a chat reply. A small local model "finishes" a
simple action ("play my Liked Songs") by chatting ("Alright, it's playing!"); the
harness tells it, in the rules, every task note, the task_complete schema and the
one re-prompt, that the sentence belongs in task_complete's result, which is the
reply the user sees. Scripted fake models; no Ollama."""

import asyncio

from modules.harness import config
from modules.harness.loop import AgentHarness, CONTINUE_PROMPT
from modules.harness.tasks import TASK_TOOL_SCHEMAS, TaskStore
from modules.llm_providers import LLMResponse, ToolCall


def call(name, **args):
    return LLMResponse(tool_calls=[ToolCall(name=name, arguments=args)])


def text(s):
    return LLMResponse(content=s)


class Script:
    def __init__(self, items):
        self.items, self.seen, self.tools = list(items), [], None

    def __call__(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        self.tools = tools
        return self.items.pop(0) if self.items else text("(script exhausted)")


def harness(tmp_path, script, dispatched):
    async def dispatch(name, args):
        dispatched.append((name, args))
        return "Playing Liked Songs on Spotify."
    return AgentHarness(script, dispatch, tools=[{"type": "function", "function": {
        "name": "spotify_play", "description": "Play music.", "parameters": {"type": "object"}}}],
        store=TaskStore(str(tmp_path / "tasks.json")), tasks_dir=str(tmp_path))


def go(h, goal="play my Liked Songs"):
    return asyncio.run(h.run(goal, [{"role": "system", "content": "SYS"}, {"role": "user", "content": goal}]))


def test_a_media_request_finishes_with_task_complete_right_after_the_tool(tmp_path):
    played = []
    s = Script([call("task_next"), call("spotify_play", query="Liked Songs"),
                call("task_complete", id=1, result="Done, your Liked Songs are playing.")])
    res = go(harness(tmp_path, s, played))
    assert res.termination_reason == "task_complete" and res.steps == 3
    assert played == [("spotify_play", {"query": "Liked Songs"})]
    assert res.final_text == "Done, your Liked Songs are playing."     # the result IS her reply


def test_a_chatty_finish_after_the_work_finishes_the_task(tmp_path):
    """The stall from the field: she plays the music, then says so in prose."""
    played = []
    s = Script([call("task_next"), call("spotify_play", query="Liked Songs"),
                text("Alright, your Liked Songs are playing now!")])
    h = harness(tmp_path, s, played)
    res = go(h)
    assert res.termination_reason == "task_complete" and res.steps == 3       # no re-prompt, no stall
    assert res.final_text == "Alright, your Liked Songs are playing now!"
    assert h.store.list()[0]["status"] == "done" and h.finished_by_reply == 1
    assert not any(m.get("content") == CONTINUE_PROMPT for m in h.messages)


def test_she_can_skip_task_next(tmp_path):
    s = Script([call("spotify_play", query="Liked Songs"), text("Playing your Liked Songs on Spotify now.")])
    h = harness(tmp_path, s, [])
    assert go(h).termination_reason == "task_complete"
    assert h.store.list()[0]["result"] == "Playing your Liked Songs on Spotify now."


def test_prose_before_any_work_is_reprompted_not_finished(tmp_path):
    s = Script([call("task_next"), text("Sure! Playing your Liked Songs now."),
                call("spotify_play", query="Liked Songs"), call("task_complete", id=1, result="Playing them.")])
    h = harness(tmp_path, s, [])
    res = go(h)
    assert res.termination_reason == "task_complete" and h.finished_by_reply == 0
    reprompt = s.seen[2][-2]["content"]                                 # before the task note
    assert reprompt == CONTINUE_PROMPT
    assert "task_complete" in reprompt and "result" in reprompt and "shown" in reprompt


def test_a_reply_that_promises_more_work_gets_one_reprompt_first(tmp_path):
    s = Script([call("task_next"), call("spotify_play", query="Liked Songs"),
                text("Let me check it actually started."), text("Yep, it's playing.")])
    h = harness(tmp_path, s, [])
    res = go(h)
    assert any(m.get("content") == CONTINUE_PROMPT for m in h.messages)    # re-prompted once
    assert res.termination_reason == "task_complete" and res.final_text == "Yep, it's playing."


def test_two_unfinished_replies_after_the_work_finish_instead_of_stalling(tmp_path):
    s = Script([call("task_next"), call("spotify_play", query="Liked Songs"),
                text("Let me double-check."), text("I'll keep an eye on it.")])
    res = go(harness(tmp_path, s, []))
    assert res.termination_reason == "task_complete" and res.final_text == "I'll keep an eye on it."


def test_a_failed_tool_doesnt_count_as_the_work(tmp_path):
    async def broken(name, args):
        return "Error: Spotify isn't connected."
    s = Script([call("task_next"), call("spotify_play", query="x"),
                text("Hmm, that didn't work."), text("Sorry, I can't play that.")])
    h = AgentHarness(s, broken, tools=[], store=TaskStore(str(tmp_path / "t.json")), tasks_dir=str(tmp_path),
                     failure_fn=lambda r: str(r).startswith("Error"))
    res = go(h)
    assert res.termination_reason == "model_stalled" and h.store.list()[0]["status"] == "in_progress"


def test_a_question_back_to_the_user_isnt_a_finish(tmp_path):
    s = Script([call("task_next"), call("spotify_play", query="Liked Songs"),
                text("Which speaker should I use?"), call("task_complete", id=1, result="Playing.")])
    h = harness(tmp_path, s, [])
    go(h)
    assert h.finished_by_reply == 0


def test_task_tools_alone_dont_count_as_the_work(tmp_path):
    s = Script([call("task_next"), call("task_list"), text("All set!"), text("Really, all set.")])
    h = harness(tmp_path, s, [])
    assert go(h).termination_reason == "model_stalled" and h.finished_by_reply == 0


def test_every_step_reminds_her_how_to_finish(tmp_path):
    s = Script([call("task_complete", id=1, result="ok")])
    go(harness(tmp_path, s, []))
    note = s.seen[0][-1]["content"]
    assert note.startswith("[TASK STORE") and "call task_complete" in note and "never announce it in a text reply" in note
    tc = next(t["function"] for t in s.tools if t["function"]["name"] == "task_complete")
    assert "shown to the user as your reply" in tc["description"]
    assert "Your reply to the user" in tc["parameters"]["properties"]["result"]["description"]
    assert tc is next(t["function"] for t in TASK_TOOL_SCHEMAS if t["function"]["name"] == "task_complete")


def test_lumi_md_opens_with_the_critical_rule():
    rules = config.rules_text()
    assert rules.startswith("**CRITICAL AGENT RULE:**")
    first = rules.split("\n\n", 1)[0]
    for words in ("MUST call the\n`task_complete` tool", "Do NOT write a conversational text reply",
                  "shown to them as your reply"):
        assert words in first, words


def test_the_tool_descriptions_stay_short():
    """CLAUDE.md: an agent tool's description is under two sentences."""
    import re
    tc = next(t["function"] for t in TASK_TOOL_SCHEMAS if t["function"]["name"] == "task_complete")
    assert len(re.findall(r"[.!?](\s|$)", tc["description"])) < 2
