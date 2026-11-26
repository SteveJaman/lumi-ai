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


def test_a_chatty_finish_is_steered_to_task_complete(tmp_path):
    played = []
    s = Script([call("task_next"), call("spotify_play", query="Liked Songs"),
                text("Alright, I'm playing your Liked Songs now!"),
                call("task_complete", id=1, result="Alright, I'm playing your Liked Songs now!")])
    res = go(harness(tmp_path, s, played))
    assert res.termination_reason == "task_complete"
    reprompt = s.seen[3][-2]["content"]                                 # before the task note
    assert reprompt == CONTINUE_PROMPT
    assert "task_complete" in reprompt and "result" in reprompt and "shown" in reprompt


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
