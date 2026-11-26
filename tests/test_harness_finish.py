"""Finishing with task_complete, not a chat reply. A small local model "finishes" a
simple action ("play my Liked Songs") by chatting ("Alright, it's playing!"); the
harness tells it, in the rules, every task note, the task_complete schema and the
one re-prompt, that the sentence belongs in task_complete's result, which is the
reply the user sees. Scripted fake models; no Ollama."""

import asyncio
import os

from modules.harness import config
from modules.harness.loop import AgentHarness, CONTINUE_PROMPT, TOOL_THEN_TEXT_PROMPT
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
    nudges = [m for m in h.messages if m.get("content") == TOOL_THEN_TEXT_PROMPT]
    assert len(nudges) == 1 and nudges[0]["role"] == "system"            # the tool-then-text note, once
    assert "MUST call `task_complete`" in TOOL_THEN_TEXT_PROMPT and "Do not reply with text" in TOOL_THEN_TEXT_PROMPT
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


def test_her_own_stop_notes_are_never_her_finishing_reply(tmp_path):
    """Chat history holds lines like "(I stopped: model stalled...)"; a small model
    copies them. They must never become the reply that closes a task."""
    from modules.harness.loop import sounds_unfinished
    for note in ('(I stopped: model stalled. Open tasks are saved; say "continue".)',
                 "I couldn't finish that: unknown error",
                 "Something went wrong talking to my own model: timeout"):
        assert sounds_unfinished(note), note
    s = Script([call("task_next"), call("spotify_play", query="Liked Songs"),
                text('(I stopped: model stalled. Open tasks are saved; say "continue".)'),
                call("task_complete", id=1, result="Playing your Liked Songs.")])
    h = harness(tmp_path, s, [])
    res = go(h)
    assert h.finished_by_reply == 0 and res.final_text == "Playing your Liked Songs."


def test_switching_tasks_clears_the_old_tasks_messages(tmp_path):
    """Two tasks in one run (she split the request): the second can't see the
    first one's Spotify chatter."""
    store = TaskStore(str(tmp_path / "tasks.json"))
    s = Script([call("task_next"), call("spotify_play", query="Liked Songs"),
                call("task_add", description="find chinese food in Duluth"),
                call("task_complete", id=1, result="Playing."),
                call("task_next"), call("web_search", query="chinese food duluth"),
                call("task_complete", id=2, result="Here are three places.")])

    async def dispatch(name, args):
        return "ok"
    h = AgentHarness(s, dispatch, tools=[], store=store, tasks_dir=str(tmp_path))
    history = [{"role": "system", "content": "SYS"},
               {"role": "assistant", "content": "Alright, Liked Songs are on!"},
               {"role": "user", "content": "play my Liked Songs, then find chinese food in Duluth"}]
    res = asyncio.run(h.run("play my Liked Songs, then find chinese food in Duluth", history))
    assert res.termination_reason == "task_complete" and h.task_switches == 1
    after = s.seen[5]                                          # the call right after the switch
    text = " ".join(str(m.get("content")) for m in after[2:])             # after the system prompt + rules
    assert len(after) == 4 and "spotify" not in text.lower() and "Liked Songs" not in text
    assert after[0]["content"] == "SYS" and after[1]["content"].startswith("[LUMI.md RULES]")
    assert "[New task #2] find chinese food in Duluth" in after[2]["content"]


def _stale_store(tmp_path):
    """What the field log had: last night's requests still open."""
    store = TaskStore(str(tmp_path / "tasks.json"))
    store.add("find some good chinese restaurant in Duluth GA")
    store.add("resume my spotify music")
    store.next()                                               # #1 in progress, #2 pending
    return store


def test_a_new_request_parks_old_open_tasks_and_runs_first(tmp_path):
    store = _stale_store(tmp_path)
    s = Script([call("task_next"), call("spotify_play", query="Liked Songs"),
                call("task_complete", id=3, result="Playing your Liked Songs.")])

    async def dispatch(name, args):
        return "ok"
    h = AgentHarness(s, dispatch, tools=[], store=store, tasks_dir=str(tmp_path))
    res = asyncio.run(h.run("Lumi can you go play my liked song please",
                            [{"role": "system", "content": "SYS"},
                             {"role": "user", "content": "Lumi can you go play my liked song please"}]))
    assert res.termination_reason == "task_complete" and res.steps == 3      # straight to it, then stop
    assert res.final_text == "Playing your Liked Songs."
    status = {t["id"]: t["status"] for t in store.list()}
    assert status == {1: "parked", 2: "parked", 3: "done"} and sorted(h.parked_ids) == [1, 2]
    assert "Current task (in progress): #3" in s.seen[1][-1]["content"]       # never #1


def test_continue_brings_parked_tasks_back(tmp_path):
    store = _stale_store(tmp_path)
    store.park_open()
    assert not store.has_open() and store.has_parked()
    s = Script([call("task_next"), call("task_complete", id=1, result="found 3"),
                call("task_next"), call("task_complete", id=2, result="resumed")])

    async def dispatch(name, args):
        return "ok"
    h = AgentHarness(s, dispatch, tools=[], store=store, tasks_dir=str(tmp_path))
    res = asyncio.run(h.run("continue", [{"role": "system", "content": "SYS"}]))
    assert sorted(h.resumed_ids) == [1, 2] and res.termination_reason == "task_complete"
    assert {t["status"] for t in store.list()} == {"done"} and len(store.list()) == 2   # "continue" added nothing


def test_parked_tasks_dont_count_as_open():
    import tempfile
    store = TaskStore(os.path.join(tempfile.mkdtemp(), "t.json"))
    store.add("old")
    assert store.park_open() == [1]
    assert not store.has_open() and store.peek_next() is None and store.next() is None
    assert store.unpark_all() == [1] and store.peek_next()["id"] == 1


def test_the_first_task_keeps_its_chat_context(tmp_path):
    """No switch on the run's own first task: "it" and "that one" still make sense."""
    s = Script([call("task_next"), call("spotify_play", query="it"), call("task_complete", id=1, result="ok")])
    h = harness(tmp_path, s, [])
    asyncio.run(h.run("play it again", [{"role": "system", "content": "SYS"},
                                        {"role": "assistant", "content": "That was Idol by YOASOBI."},
                                        {"role": "user", "content": "play it again"}]))
    assert h.task_switches == 0
    assert any("Idol by YOASOBI" in str(m.get("content")) for m in s.seen[2])


def test_a_successful_action_counts_as_progress(tmp_path):
    """Field log: spotify_play worked, then she checked what was playing, and the
    run ended 'stagnant' because only file/command tools counted as progress."""
    s = Script([call("task_next"), call("spotify_play", query="Liked Songs")]
               + [call("spotify_now_playing")] * 4 + [call("task_complete", id=1, result="Playing.")])
    res = go(harness(tmp_path, s, []))
    assert res.termination_reason == "task_complete"


def test_only_looking_is_still_stagnant(tmp_path):
    s = Script([call("task_next")] + [call("spotify_now_playing")] * 6)
    assert go(harness(tmp_path, s, [])).termination_reason == "stagnant"


def test_what_counts_as_progress():
    from modules.harness.loop import is_progress
    mutating = {"write_file", "run_command"}
    assert is_progress("spotify_play", False, mutating) and is_progress("click_button", False, mutating)
    assert is_progress("write_file", True, mutating)                 # mutating tools, as before
    for looking in ("spotify_now_playing", "web_search", "browse_url", "read_file", "media_playlist_tracks",
                    "get_active_window_info", "task_next", "task_list"):
        assert not is_progress(looking, False, mutating), looking
    assert not is_progress("spotify_play", True, mutating)           # a failed action isn't progress
