"""Persistent task DAG (modules/harness/tasks.py)."""

import json
import os

import pytest

from modules.harness import config
from modules.harness.tasks import (
    DAGError, TaskStore, TASK_TOOL_SCHEMAS, execute_task_tool, validate,
)


@pytest.fixture
def store(tmp_path):
    return TaskStore(str(tmp_path / "lumi_tasks.json"))


def test_add_and_next_respects_dependencies(store):
    a = store.add("write the file")
    b = store.add("run the file", depends_on=[a["id"]])
    assert store.next()["id"] == a["id"]
    # a is in progress: next returns it again (resume), never b
    assert store.next()["id"] == a["id"]
    store.complete(a["id"], "written")
    nxt = store.next()
    assert nxt["id"] == b["id"] and nxt["status"] == "in_progress" and nxt["attempts"] == 1
    store.complete(b["id"], "ran")
    assert store.next() is None
    assert not store.has_open()


def test_rejects_missing_dependency(store):
    with pytest.raises(DAGError, match="unknown task"):
        store.add("x", depends_on=[42])
    assert store.list() == []          # nothing written


def test_rejects_cycle():
    tasks = [
        {"id": 1, "status": "pending", "depends_on": [2]},
        {"id": 2, "status": "pending", "depends_on": [3]},
        {"id": 3, "status": "pending", "depends_on": [1]},
    ]
    with pytest.raises(DAGError, match="cycle"):
        validate(tasks)
    with pytest.raises(DAGError, match="itself"):
        validate([{"id": 1, "status": "pending", "depends_on": [1]}])


def test_rejects_hand_edited_cycle_on_next_write(store, tmp_path):
    store.add("a")
    store.add("b")
    data = json.loads(open(store.path, encoding="utf-8").read())
    data["tasks"][0]["depends_on"] = [2]
    data["tasks"][1]["depends_on"] = [1]
    open(store.path, "w", encoding="utf-8").write(json.dumps(data))
    with pytest.raises(DAGError, match="cycle"):
        store.add("c")


def test_fail_blocks_dependents_transitively(store):
    a = store.add("a")
    b = store.add("b", depends_on=[a["id"]])
    c = store.add("c", depends_on=[b["id"]])
    d = store.add("d")
    store.next()
    store.fail(a["id"], "boom")
    st = {t["id"]: t["status"] for t in store.list()}
    assert st == {a["id"]: "failed", b["id"]: "blocked", c["id"]: "blocked", d["id"]: "pending"}
    assert store.get(a["id"])["last_error"] == "boom"
    assert store.next()["id"] == d["id"]


def test_persists_across_instances(store):
    a = store.add("survive a restart", acceptance_criteria="still there")
    store.next()
    again = TaskStore(store.path)
    cur = again.current()
    assert cur["id"] == a["id"] and cur["status"] == "in_progress"
    assert again.next()["id"] == a["id"]


def test_atomic_write_leaves_no_temp(store):
    store.add("a")
    assert os.path.exists(store.path)
    assert not os.path.exists(store.path + ".tmp")


def test_tools_roundtrip(store):
    out = json.loads(execute_task_tool(store, "task_add",
                                       {"description": "a", "acceptance_criteria": "ok"}))
    tid = out["added"]["id"]
    assert json.loads(execute_task_tool(store, "task_next", {}))["task"]["id"] == tid
    assert json.loads(execute_task_tool(store, "task_complete", {"id": str(tid), "result": "r"}))["completed"]["status"] == "done"
    assert json.loads(execute_task_tool(store, "task_next", {}))["task"] is None
    assert execute_task_tool(store, "task_add", {"description": "x", "depends_on": [99]}).startswith("Error:")
    assert execute_task_tool(store, "task_complete", {"id": tid, "result": "again"}).startswith("Error:")
    listing = json.loads(execute_task_tool(store, "task_list", {"status": "done"}))
    assert [t["id"] for t in listing] == [tid]


def test_tool_schemas_are_short_and_complete():
    names = {s["function"]["name"] for s in TASK_TOOL_SCHEMAS}
    assert names == {"task_list", "task_add", "task_next", "task_complete", "task_fail"}
    for s in TASK_TOOL_SCHEMAS:
        fn = s["function"]
        assert fn["parameters"]["type"] == "object"
        assert fn["description"].count(". ") < 2 and len(fn["description"]) < 200


def test_lumi_md_model_section(tmp_path):
    p = tmp_path / "LUMI.md"
    p.write_text("# x\n\n## Rules\n\n1. a\n\n## Model\n\n- agent_model: foo:14b\n- num_ctx: 12288\n",
                 encoding="utf-8")
    assert config.agent_model(str(p)) == "foo:14b"
    assert config.num_ctx(str(p)) == 12288
    assert config.rules_text(str(p)).startswith("1. a")
    empty = tmp_path / "E.md"
    empty.write_text("## Model\n\nTBD\n", encoding="utf-8")
    from constants import AGENT_MODEL_NAME, MAIN_NUM_CTX
    assert config.agent_model(str(empty)) == AGENT_MODEL_NAME
    assert config.num_ctx(str(empty)) == MAIN_NUM_CTX
