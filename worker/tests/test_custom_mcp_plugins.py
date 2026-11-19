"""Exercise custom tools via their public CLI, embedded operator, and a real stdio process."""

import json
import sys
from pathlib import Path

def _fixture_server(journal):
    schema = {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]}
    specs = [
        {
            "name": name,
            "inputSchema": schema,
            "annotations": {"readOnlyHint": read_only, "destructiveHint": False},
        }
        for name, read_only in (("check", True), ("record", False))
    ]
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        if method == "initialize":
            result = {
                "protocolVersion": request["params"]["protocolVersion"],
                "serverInfo": {"name": "fixture", "version": "1"},
                "capabilities": {"tools": {}},
            }
        elif method == "tools/list":
            result = {"tools": specs}
        elif method == "tools/call":
            name = request["params"]["name"]
            label = request["params"]["arguments"]["label"]
            with journal.open("a", encoding="utf-8") as output:
                output.write(json.dumps([name, label]) + "\n")
            if label == "drop-reply" and name == "record":
                return  # The side effect happened before the transport disconnected.
            output = {"label": label, "eligible": name == "check" and label == "ready",
                      "private_note": "external-fixture-private-output"}
            result = {"content": [{"type": "text", "text": json.dumps(output)}], "structuredContent": output}
        elif method in {"resources/list", "prompts/list"}:
            result = {method.split("/")[0]: []}
        else:
            raise AssertionError(method)
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


if __name__ == "__main__":
    _fixture_server(Path(sys.argv[1]))
    sys.exit(0)


import pytest

from megas.action_skills import ActionSkills
from megas.actions import MegasActions
from megas.custom_mcp_plugins import CustomMCPPlugins, call_direct


class PluginFixture:
    def __init__(self, tmp_path):
        self.home = tmp_path
        self.owner = "fixture-owner-a"
        self.journal = tmp_path / "calls.jsonl"
        self.cli("add", "fixture", "--command", sys.executable,
                 "--arg", str(Path(__file__).resolve()), "--arg", str(self.journal))
        self.cli("probe", "fixture", "--timeout", "5")

    def plugins(self):
        return CustomMCPPlugins(current_owner=lambda: self.owner, home=self.home)

    def cli(self, *args):
        self.plugins().manage(args)

    def capabilities(self):
        rows = self.call("megas_skill_capabilities")["plugins"]
        return {row["tool"]: row for row in rows if row.get("server") == "fixture"}

    def call(self, tool, **args):
        plugins = self.plugins()
        if tool == "megas_plugin_call":
            reply = call_direct(args, plugins)
        elif tool.startswith("megas_skill_"):
            reply = ActionSkills(owner=lambda: self.owner, plugins=plugins, home=self.home).call(tool, args)
        else:
            reply = MegasActions(self.home).call(tool, args)
        assert not reply.get("isError"), reply
        return reply["structuredContent"]

    def assert_denied(self, tool, **args):
        try:
            self.call(tool, **args)
        except (PermissionError, KeyError, ValueError, RuntimeError):
            return
        pytest.fail("Unauthorized tool call succeeded")

    def entries(self):
        if not self.journal.exists():
            return []
        return [json.loads(line) for line in self.journal.read_text(encoding="utf-8").splitlines()]

    def save(self, skills, *, read_label, write_label):
        return self.save_steps([
            {"tool": "plugin_read", "name": skills["check"]["name"], "arguments": {"label": read_label}},
            {
                "tool": "megas_action_task_create",
                "title": "Review fixture output",
                "when": {"path": ["eligible"], "operator": "equals", "value": True},
            },
            {"tool": "plugin_call", "name": skills["record"]["name"], "arguments": {"label": write_label}},
        ])

    def save_steps(self, steps):
        action = self.call("megas_action_create", name="MCP fixture", description="", paths=[])
        self.call(
            "megas_skill_save", skill_id="rollup", name="Act on the check", action_id=action["id"],
            expected_revision=0, enabled=True, trigger={"kind": "manual"}, steps=steps,
        )
        return action["id"]


@pytest.fixture
def plugin(tmp_path):
    return PluginFixture(tmp_path)


def test_grant_revoke_and_owner_switch(plugin):
    assert not plugin.capabilities()
    plugin.cli("allow", "fixture", "check")
    catalog = plugin.capabilities()
    assert set(catalog) == {"check"}
    check = catalog["check"]
    assert check["name"].startswith("mcp_") and check["read_only"] is True and check["grant"]
    args = {"name": check["name"], "arguments": {"label": "ready"}}
    plugin.call("megas_plugin_call", **args)
    assert plugin.entries() == [["check", "ready"]]

    plugin.owner = "fixture-owner-b"
    assert not plugin.capabilities()
    plugin.assert_denied("megas_plugin_call", **args)
    plugin.owner = "fixture-owner-a"
    assert plugin.capabilities()["check"]["name"] == check["name"]
    plugin.cli("deny", "fixture", "check")
    assert not plugin.capabilities()
    plugin.assert_denied("megas_plugin_call", **args)
    assert plugin.entries() == [["check", "ready"]]


@pytest.mark.parametrize(
    ("read_label", "write_label", "expected_tasks", "expected_state"),
    [("ready", "accepted", 1, "completed"), ("waiting", "drop-reply", 0, "delivery_unconfirmed")],
)
def test_conditional_skill_does_not_replay_external_side_effect(
    plugin, read_label, write_label, expected_tasks, expected_state
):

    for name in ("check", "record"):
        plugin.cli("allow", "fixture", name)
    grants = plugin.capabilities()
    assert grants["record"]["read_only"] is False
    action_id = plugin.save(grants, read_label=read_label, write_label=write_label)
    run = {"skill_id": "rollup", "request_id": "one"}
    result = plugin.call("megas_skill_run", **run)
    assert result["steps"][-1]["state"] == expected_state
    assert plugin.entries() == [["check", read_label], ["record", write_label]]
    assert len(plugin.call("megas_action_get", action_id=action_id)["tasks"]) == expected_tasks

    replay = plugin.call("megas_skill_run", **run)
    assert replay["run_id"] == result["run_id"] and replay["steps"][-1]["state"] == expected_state
    assert plugin.entries() == [["check", read_label], ["record", write_label]]
    plugin.owner = "fixture-owner-b"
    plugin.assert_denied("megas_skill_run", **run)
    assert plugin.entries() == [["check", read_label], ["record", write_label]]


def test_saved_external_effect_cannot_spend_a_replacement_grant(plugin):
    for tool in ("check", "record"):
        plugin.cli("allow", "fixture", tool)
    granted = plugin.capabilities()
    action_id = plugin.save(granted, read_label="ready", write_label="old-grant")
    plugin.cli("deny", "fixture", "record")
    plugin.cli("allow", "fixture", "record")
    assert plugin.capabilities()["record"]["grant"] != granted["record"]["grant"]
    with pytest.raises(PermissionError, match="grant changed"):
        plugin.call("megas_skill_run", skill_id="rollup", request_id="after-regrant")
    assert plugin.call("megas_action_get", action_id=action_id)["tasks"] == []
    assert plugin.entries() == [["check", "ready"]]


def test_concurrent_ordered_clients_never_replay_plugin_write(plugin):
    from concurrent.futures import ThreadPoolExecutor

    plugin.cli("allow", "fixture", "record")
    name = plugin.capabilities()["record"]["name"]
    action_id = plugin.save_steps([
        {"tool": "plugin_call", "name": name, "arguments": {"label": "one"}},
        {"tool": "megas_action_task_create", "title": "Once"},
    ])
    run = {"skill_id": "rollup", "request_id": "together"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: plugin.call("megas_skill_run", **run), range(4)))
    receipt = plugin.call("megas_skill_run", **run)
    assert [s["state"] for s in receipt["steps"]] == ["completed", "completed"]
    assert len(plugin.call("megas_action_get", action_id=action_id)["tasks"]) == 1
    assert plugin.entries() == [["record", "one"]]


def test_revoking_prior_grant_blocks_delayed_local_step(plugin, monkeypatch):
    from megas.action_skills import ActionSkills

    plugin.cli("allow", "fixture", "record")
    name = plugin.capabilities()["record"]["name"]
    action = plugin.save_steps([
        {"tool": "plugin_call", "name": name, "arguments": {"label": "one"}},
        {"tool": "megas_action_task_create", "title": "Dependent task"},
    ])
    args = {"skill_id": "rollup", "request_id": "revoked"}
    advance = ActionSkills._sequence_step

    def interrupt(self, uid, key, run_id, index, guard):
        if index == 1:
            raise RuntimeError("operator disconnected")
        return advance(self, uid, key, run_id, index, guard)

    with monkeypatch.context() as patch:
        patch.setattr(ActionSkills, "_sequence_step", interrupt)
        with pytest.raises(RuntimeError, match="operator disconnected"):
            plugin.call("megas_skill_run", **args)
    plugin.cli("deny", "fixture", "record")
    result = plugin.call("megas_skill_run", **args)
    assert [s["state"] for s in result["steps"]] == ["completed", "cancelled_before_delivery"]
    assert plugin.entries() == [["record", "one"]]
    assert not plugin.call("megas_action_get", action_id=action)["tasks"]
