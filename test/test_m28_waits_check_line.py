import copy
import json
import unittest
from unittest.mock import Mock, patch

from mcp.server.mcpserver.exceptions import ToolError

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import ITEMS_STALLED_HINTS, RESEARCH_STALLED_HINT, create_server


def payload(result):
    return json.loads(result.content[0].text)


LINE = {
    "item": "transport-belt",
    "surface": "nauvis",
    "from_x": 0.5,
    "from_y": 2.5,
    "to_x": 4.5,
    "to_y": 2.5,
}


class WaitPositionTests(unittest.IsolatedAsyncioTestCase):
    async def run_wait(
        self, tool, *, end=(3, 4), failures=(), outcome="timeout", stall_seconds=None
    ):
        elapsed = 0.0
        position_reads = 0

        async def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        def read(operation, params):
            nonlocal position_reads
            if operation == "get_character_position":
                position_reads += 1
                if position_reads in failures:
                    raise BridgeError("not_connected", "Optional read failed")
                x, y = (0, 0) if position_reads == 1 else end
                return {"result": {"position": {"x": x, "y": y, "surface": "nauvis"}}}
            if operation == "get_research_status":
                value = {"current": "automation", "progress": 0.25}
                if outcome == "reached":
                    value = {"researched": ["automation"]}
                elif outcome == "not_queued":
                    value = {}
                return {"result": value}
            if operation == "research_labs":
                return {"result": {"technology": "automation", "labs": [{"missing": []}]}}
            if operation == "entity_feeders":
                return {"result": {"feeders": {}}}
            self.assertEqual(operation, "get_entity_inventory")
            return {
                "result": {
                    "name": "assembling-machine-1",
                    "status": "no_power" if outcome == "stalled" else "working",
                    "inventories": {
                        "result": [{"name": "iron-plate", "count": int(outcome == "reached")}]
                    },
                }
            }

        bridge = Mock()
        bridge.call.side_effect = read
        arguments = (
            {"technology": "automation"}
            if tool == "wait_for_research"
            else {"target_ref": "chest", "item": "iron-plate", "count": 1}
        )
        arguments["timeout_seconds"] = 20 if outcome == "stalled" else 12
        if stall_seconds is not None:
            arguments["stall_seconds"] = stall_seconds
        with (
            patch("factorio_mcp.server.time") as clock,
            patch("factorio_mcp.server.asyncio.sleep", side_effect=advance),
        ):
            clock.monotonic.side_effect = lambda: elapsed
            response = await create_server(bridge).call_tool(tool, arguments)
        self.assertFalse(response.is_error, payload(response))
        self.assertEqual(position_reads, 2)
        self.assertEqual(bridge.call.call_args_list[0].args[0], "get_character_position")
        self.assertEqual(bridge.call.call_args_list[-1].args[0], "get_character_position")
        return payload(response)

    async def test_positions_and_drift_for_every_successful_outcome(self):
        for tool in ("wait_for_items", "wait_for_research"):
            outcomes = ["reached", "timeout", "stalled"]
            if tool == "wait_for_research":
                outcomes.append("not_queued")
            for outcome in outcomes:
                with self.subTest(tool=tool, outcome=outcome):
                    value = await self.run_wait(
                        tool,
                        outcome=outcome,
                        stall_seconds=(
                            15 if tool == "wait_for_research" and outcome == "timeout" else None
                        ),
                    )
                    self.assertEqual(value["character_position"], {"x": 3, "y": 4})
                    self.assertEqual(
                        value["position_drift"],
                        {"from": {"x": 0, "y": 0}, "to": {"x": 3, "y": 4}, "distance": 5},
                    )
                    self.assertIn("may have been carried by a belt", value["hint"])
                    self.assertIn("stand off belts before the next action", value["hint"])
                    if outcome == "stalled":
                        existing = (
                            RESEARCH_STALLED_HINT
                            if tool == "wait_for_research"
                            else ITEMS_STALLED_HINTS["no_power"]
                        )
                        self.assertTrue(value["hint"].startswith(existing + " "))

    async def test_small_movement_does_not_report_drift_or_hint(self):
        for tool in ("wait_for_items", "wait_for_research"):
            for end in ((0, 0), (0.3, 0.4), (-0.5, 0)):
                with self.subTest(tool=tool, end=end):
                    value = await self.run_wait(tool, end=end, outcome="reached")
                    self.assertEqual(value["character_position"], {"x": end[0], "y": end[1]})
                    self.assertNotIn("position_drift", value)
                    self.assertNotIn("hint", value)

    async def test_position_read_failure_preserves_wait(self):
        for tool in ("wait_for_items", "wait_for_research"):
            for failures in ((1,), (2,), (1, 2)):
                with self.subTest(tool=tool, failures=failures):
                    value = await self.run_wait(tool, outcome="reached", failures=failures)
                    self.assertNotIn("position_drift", value)
                    self.assertNotIn("hint", value)
                    self.assertNotIn("character_position", value)

    async def test_research_stall_threshold_is_used_and_default_unchanged(self):
        value = await self.run_wait("wait_for_research", stall_seconds=15)
        self.assertEqual(value["waited_seconds"], 12)
        self.assertFalse(value["stalled"])
        self.assertTrue(value["timed_out"])
        for threshold in (None, 5, 15, 19.5):
            with self.subTest(threshold=threshold):
                value = await self.run_wait(
                    "wait_for_research", outcome="stalled", stall_seconds=threshold
                )
                self.assertTrue(value["stalled"])
                self.assertFalse(value["timed_out"])
                self.assertEqual(value["stalled_seconds"], threshold or 10)

    async def test_research_stall_validation(self):
        bridge = Mock()
        server = create_server(bridge)
        for threshold in (4, 51, float("inf"), float("nan"), None, [], "bad"):
            with self.subTest(threshold=threshold), self.assertRaises(ToolError) as error:
                await server.call_tool(
                    "wait_for_research", {"technology": "automation", "stall_seconds": threshold}
                )
            self.assertEqual(json.loads(str(error.exception))["error"], "invalid_input")
        bridge.call.assert_not_called()
        bridge.call.return_value = {"result": {"researched": ["automation"]}}
        for threshold in (5, 50):
            response = await server.call_tool(
                "wait_for_research", {"technology": "automation", "stall_seconds": threshold}
            )
            self.assertFalse(response.is_error)


class CheckLineTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_tools_and_descriptions(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        self.assertNotIn("get_character_position", tools)
        self.assertEqual(set(tools["check_line"].input_schema["required"]), set(LINE))
        for phrase in (
            "place_line",
            "trees, water",
            "mine_entity",
            "walking or mining",
            "No reach check",
            "not_visible",
            "observe_nearby first",
            "2-64 tiles",
            "transport-belt or pipe",
            "tile centres",
            "problem_total",
        ):
            self.assertIn(phrase, tools["check_line"].description)
        for tool in ("wait_for_items", "wait_for_research"):
            for phrase in ("character_position", "position_drift", "0.5 tiles", "belt"):
                self.assertIn(phrase, tools[tool].description)
        for tool in ("move_to", "wait_for_action"):
            self.assertIn("standing_on_belt", tools[tool].description)
        self.assertIn("science packs arrive with gaps", tools["wait_for_research"].description)
        schema = tools["wait_for_research"].input_schema["properties"]["stall_seconds"]
        self.assertEqual((schema["minimum"], schema["maximum"], schema["default"]), (5, 50, 10))

    async def test_invalid_arguments_never_call_bridge(self):
        bridge = Mock()
        server = create_server(bridge)
        invalid = []
        for key in ("item", "surface"):
            invalid.extend({**LINE, key: v} for v in (None, "", True, 3, []))
        for key in ("from_x", "from_y", "to_x", "to_y"):
            invalid.extend(
                {**LINE, key: v}
                for v in (None, "1", True, [], float("nan"), float("inf"), -1000001, 1000001)
            )
        invalid.extend([{**LINE, "to_y": 3.5}, {**LINE, "to_x": 0.9}, {**LINE, "to_x": 64.5}])
        invalid.extend({k: v for k, v in LINE.items() if k != key} for key in LINE)
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                try:
                    response = await server.call_tool("check_line", arguments)
                except ToolError as exc:
                    value = json.loads(str(exc))
                else:
                    self.assertTrue(response.is_error)
                    value = payload(response)
                self.assertEqual(value["error"], "invalid_input")
        bridge.call.assert_not_called()

    async def test_passthrough_and_lua_arrays(self):
        measured = {
            "requested": 5,
            "clear": 2,
            "already_present": 1,
            "direction": 4,
            "complete": False,
            "first_position": {"x": 0.5, "y": 2.5},
            "last_position": {"x": 4.5, "y": 2.5},
            "problem_total": 2,
            "problems": [
                {
                    "x": 2.5,
                    "y": 2.5,
                    "reason": "blocked",
                    "blocking": [{"name": "tree-01", "target_ref": "42"}],
                    "tiles": {},
                },
                {"x": 4.5, "y": 2.5, "reason": "not_visible"},
            ],
        }
        bridge = Mock()
        bridge.call.return_value = {"tick": 42, "result": copy.deepcopy(measured)}
        server = create_server(bridge)
        for overrides in (
            {},
            {"from_x": -0.1, "to_x": -63.1},
            {"to_x": 0.5, "to_y": -60.5},
            {"to_x": 63.9},
        ):
            args = {**LINE, **overrides, "surface": "custom"}
            response = await server.call_tool("check_line", args)
            self.assertFalse(response.is_error)
            bridge.call.assert_called_with("check_line", args)
        measured["problems"][0]["tiles"] = []
        self.assertEqual(payload(response), {"ok": True, "tick": 42, **measured})
        bridge.call.return_value = {"result": {"problems": {}, "problem_total": 0}}
        self.assertEqual(payload(await server.call_tool("check_line", LINE))["problems"], [])
        bridge.call.side_effect = BridgeError(
            "invalid_target", "Unsupported", {"reason": "not_line_placeable"}
        )
        response = await server.call_tool("check_line", {**LINE, "item": "wooden-chest"})
        self.assertTrue(response.is_error)
        self.assertEqual(payload(response)["detail"], {"reason": "not_line_placeable"})

    async def test_successful_move_belt_metadata_passes_through_both_wait_paths(self):
        completed = {
            "action_id": "move-1",
            "type": "move",
            "status": "succeeded",
            "standing_on_belt": True,
            "hint": "Stand off belts.",
        }
        for metadata in ({}, {"standing_on_belt": True, "hint": completed["hint"]}):
            result = {k: v for k, v in completed.items() if k not in ("standing_on_belt", "hint")}
            result.update(metadata)
            for mode in ("immediate", "move_wait", "action_wait"):
                with self.subTest(metadata=metadata, mode=mode):
                    bridge = Mock()
                    bridge.call.side_effect = lambda op, params, mode=mode, result=result: {
                        "result": (
                            {"action_id": "move-1", "status": "running"}
                            if op == "move_to" and mode == "move_wait"
                            else result
                        )
                    }
                    server = create_server(bridge)
                    if mode == "action_wait":
                        response = await server.call_tool(
                            "wait_for_action", {"action_id": "move-1"}
                        )
                    else:
                        args = {"surface": "nauvis", "x": 0.5, "y": 0.5}
                        if mode == "move_wait":
                            args["wait_seconds"] = 1
                        response = await server.call_tool("move_to", args)
                    self.assertFalse(response.is_error)
                    value = payload(response)
                    if metadata:
                        self.assertTrue(value["standing_on_belt"])
                        self.assertEqual(value["hint"], metadata["hint"])
                    else:
                        self.assertNotIn("standing_on_belt", value)
                        self.assertNotIn("hint", value)
