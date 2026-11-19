import copy
import json
import unittest
from unittest.mock import Mock, patch

from mcp.server.mcpserver.exceptions import ToolError

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import create_server


def payload(response):
    return json.loads(response.content[0].text)


PLACE = {"item": "inserter", "surface": "nauvis", "x": 30.5, "y": 0.5, "direction": 12}
TRANSFER = {
    "target_ref": "chest",
    "inventory_role": "main",
    "direction": "to_entity",
    "item": "coal",
    "count": 3,
}
TOOLS = {
    "place_entities": {"items": [PLACE]},
    "transfer_items": {"items": [TRANSFER]},
    "mine_entities": {"target_refs": ["a", "b"]},
}


class TargetsPumpWalkTests(unittest.IsolatedAsyncioTestCase):
    async def test_walk_schema_strict_bool_and_wait_bounds(self):
        bridge = Mock()
        server = create_server(bridge)
        tools = {tool.name: tool for tool in await server.list_tools()}
        for name, args in TOOLS.items():
            properties = tools[name].input_schema["properties"]
            self.assertEqual(
                properties["walk"], {"default": False, "title": "Walk", "type": "boolean"}
            )
            self.assertEqual(
                properties["wait_seconds"],
                tools["place_line"].input_schema["properties"]["wait_seconds"],
            )
            for walk in (0, 1, "true", "false", None):
                with self.subTest(name=name, walk=walk), self.assertRaises(ToolError):
                    await server.call_tool(name, {**args, "walk": walk})
            for seconds in (0, 51, float("nan"), float("inf")):
                with self.subTest(name=name, seconds=seconds), self.assertRaises(ToolError):
                    await server.call_tool(name, {**args, "walk": True, "wait_seconds": seconds})
            if name != "mine_entities":
                for extra in ({}, {"walk": False}):
                    response = await server.call_tool(name, {**args, **extra, "wait_seconds": 1})
                    self.assertTrue(response.is_error)
                    self.assertEqual(payload(response)["error"], "invalid_input")
        bridge.call.assert_not_called()

    async def test_mining_wait_without_walk_starts_and_polls_action(self):
        args = TOOLS["mine_entities"]
        for extra in ({}, {"walk": False}):
            for seconds in (1, 50):
                with self.subTest(extra=extra, seconds=seconds):
                    bridge = Mock()
                    final = {
                        "action_id": "m",
                        "type": "mine_entities",
                        "status": "succeeded",
                        "mined": 2,
                        "requested": 2,
                        "gained": [],
                    }
                    bridge.call.side_effect = [
                        {"result": {"action_id": "m", "status": "running"}},
                        {"result": final},
                    ]
                    response = await create_server(bridge).call_tool(
                        "mine_entities", {**args, **extra, "wait_seconds": seconds}
                    )
                    self.assertFalse(response.is_error, payload(response))
                    self.assertEqual(
                        [call.args for call in bridge.call.call_args_list],
                        [("mine_entities", args), ("get_action_status", {"action_id": "m"})],
                    )
                    for key, expected in final.items():
                        self.assertEqual(payload(response)[key], expected)

    async def test_default_and_false_keep_existing_wire_params(self):
        for name, args in TOOLS.items():
            bridge = Mock()
            bridge.call.return_value = {"result": {"action_id": "a", "status": "running"}}
            server = create_server(bridge)
            for extra in ({}, {"walk": False}, {"walk": False, "wait_seconds": None}):
                await server.call_tool(name, {**args, **extra})
                bridge.call.assert_called_with(name, args)

    async def test_walk_true_uses_action_wait_and_preserves_final_progress(self):
        for name, args in TOOLS.items():
            bridge = Mock()
            final = {
                "action_id": "a",
                "type": name,
                "status": "succeeded",
                "walks": 2,
                "current_index": 2,
            }
            if name == "mine_entities":
                final.update(mined=2, requested=2, gained=[])
            else:
                final.update(
                    results=[{"index": 1, "ok": True, "result": {"moved": 3}}],
                    succeeded=1,
                    failed=0,
                )
            bridge.call.side_effect = [
                {"result": {"action_id": "a", "type": name, "status": "running", "walks": 0}},
                {"result": copy.deepcopy(final)},
            ]
            response = await create_server(bridge).call_tool(
                name, {**args, "walk": True, "wait_seconds": 1}
            )
            self.assertFalse(response.is_error, payload(response))
            self.assertEqual(bridge.call.call_args_list[0].args, (name, {**args, "walk": True}))
            self.assertEqual(
                bridge.call.call_args_list[1].args, ("get_action_status", {"action_id": "a"})
            )
            for key, expected in final.items():
                self.assertEqual(payload(response)[key], expected)

    async def test_timeout_status_cancel_and_surface_failure_keep_partial_results(self):
        for name in ("place_entities", "transfer_items"):
            progress = {
                "action_id": "a",
                "type": name,
                "status": "running",
                "walks": 1,
                "current_index": 2,
                "succeeded": 1,
                "failed": 0,
                "results": [{"index": 1, "ok": True, "result": {"name": "wooden-chest"}}],
            }
            elapsed = [0.0]

            async def advance(seconds, timer=elapsed):
                timer[0] += seconds

            bridge = Mock()
            bridge.call.return_value = {"result": copy.deepcopy(progress)}
            server = create_server(bridge)
            with (
                patch("factorio_mcp.server.time") as clock,
                patch("factorio_mcp.server.asyncio.sleep", side_effect=advance),
            ):
                clock.monotonic.side_effect = lambda timer=elapsed: timer[0]
                result = payload(
                    await server.call_tool(name, {**TOOLS[name], "walk": True, "wait_seconds": 1})
                )
            self.assertTrue(result["timed_out"])
            self.assertEqual(result["results"], progress["results"])
            for tool, status, error in (
                ("get_action_status", "failed", "surface_changed"),
                ("get_action_status", "failed", "character_dead"),
                ("get_action_status", "failed", "connection_lost"),
                ("cancel_action", "cancelled", "cancelled"),
            ):
                value = {**progress, "status": status, "error": error}
                bridge.call.return_value = {"result": copy.deepcopy(value)}
                result = payload(await server.call_tool(tool, {"action_id": "a"}))
                for key, expected in value.items():
                    self.assertEqual(result[key], expected)

    async def test_target_warning_and_pump_passthrough(self):
        bridge = Mock()
        server = create_server(bridge)
        target = {"target_ref": "chest", "name": "wooden-chest"}
        for value in (
            {"pickup_target": target, "drop_target": target},
            {"no_drop_target": True},
            {"warnings": ["no_drop_target"], "hint": "Items will be dropped on the ground."},
        ):
            for name, args in (
                ("place_entity", PLACE),
                ("get_entity_inventory", {"target_ref": "inserter"}),
            ):
                bridge.call.return_value = {"result": copy.deepcopy(value)}
                result = payload(await server.call_tool(name, args))
                for key, expected in value.items():
                    self.assertEqual(result[key], expected)
            for name, args in (
                ("place_entities", {"items": [PLACE]}),
                ("get_entity_inventories", {"target_refs": ["inserter"]}),
            ):
                bridge.call.return_value = {
                    "result": {
                        "results": [{"index": 1, "ok": True, "result": copy.deepcopy(value)}]
                    }
                }
                result = payload(await server.call_tool(name, args))["results"][0]["result"]
                for key, expected in value.items():
                    self.assertEqual(result[key], expected)
        water = {"pump_position": {"x": 3.5, "y": 2.5, "direction": 12}}
        bridge.call.return_value = {"result": {"water": [water]}}
        self.assertEqual(payload(await server.call_tool("get_explored_map", {}))["water"], [water])
        for compact in (False, True):
            value = {"name": "inserter", "no_drop_target": True}
            bridge.call.return_value = {"result": {"entities": [value]}}
            result = payload(await server.call_tool("observe_nearby", {"compact": compact}))
            self.assertEqual(result["entities"], [value])

    async def test_action_batches_normalize_entry_hints_on_status(self):
        bridge = Mock()
        bridge.call.return_value = {
            "result": {
                "type": "place_entities",
                "status": "failed",
                "results": [{"index": 1, "ok": False, "error": "out_of_range"}],
            }
        }
        value = payload(
            await create_server(bridge).call_tool("get_action_status", {"action_id": "a"})
        )
        self.assertIn("hint", value["results"][0])

    async def test_completed_walk_batches_preserve_timeout_and_later_success(self):
        for name in ("place_entities", "transfer_items"):
            final = {
                "action_id": "a",
                "type": name,
                "status": "succeeded",
                "started_tick": 100,
                "walks": 2,
                "current_index": 3,
                "succeeded": 1,
                "failed": 1,
                "results": [
                    {"index": 1, "ok": False, "error": "timeout"},
                    {"index": 2, "ok": True, "result": {"moved": 3}},
                ],
            }
            bridge = Mock()
            bridge.call.return_value = {"result": copy.deepcopy(final)}
            response = await create_server(bridge).call_tool(
                "get_action_status", {"action_id": "a"}
            )
            self.assertFalse(response.is_error, payload(response))
            for key, expected in final.items():
                if key != "results":
                    self.assertEqual(payload(response)[key], expected)
            results = payload(response)["results"]
            self.assertEqual(results[0]["error"], "timeout")
            self.assertEqual(results[1], final["results"][1])

    async def test_conflicts_follow_walking_line_policy(self):
        for name, args in TOOLS.items():
            bridge = Mock()
            bridge.call.side_effect = BridgeError(
                "action_conflict", "busy", {"running_action": {"id": "m", "type": "mine_entities"}}
            )
            response = await create_server(bridge).call_tool(name, {**args, "walk": True})
            self.assertEqual(payload(response)["error"], "action_conflict")
            self.assertEqual(bridge.call.call_count, 1)
        for kind in ("move", "place_line", "place_entities", "transfer_items"):
            bridge = Mock()
            bridge.call.side_effect = [
                BridgeError(
                    "action_conflict", "busy", {"running_action": {"id": "w", "type": kind}}
                ),
                {"result": {"action_id": "w", "type": kind, "status": "succeeded"}},
                {"result": {"action_id": "new", "type": "place_entities", "status": "running"}},
            ]
            result = payload(
                await create_server(bridge).call_tool(
                    "place_entities", {**TOOLS["place_entities"], "walk": True}
                )
            )
            self.assertEqual(result["action_id"], "new")
            self.assertEqual(result["waited_for_walk"]["status"], "succeeded")
            self.assertEqual(bridge.call.call_count, 3)

    async def test_descriptions(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in TOOLS:
            for phrase in (
                "walk=true",
                "wait_seconds",
                "move_to",
                "walks",
                "in order",
                "cancel_action",
            ):
                self.assertIn(phrase, tools[name].description)
        for name in (
            "place_entity",
            "place_entities",
            "get_entity_inventory",
            "get_entity_inventories",
        ):
            for phrase in ("pickup_target", "drop_target", "no_drop_target", "ground"):
                self.assertIn(phrase, tools[name].description)
        self.assertIn("pump_position", tools["get_explored_map"].description)
        self.assertIn("no_drop_target", tools["observe_nearby"].description)
        self.assertNotIn("requires walk=true", tools["mine_entities"].description)
        for name in ("place_entities", "transfer_items"):
            self.assertIn("wait_seconds requires walk=true", tools[name].description)
        self.assertNotIn("six shore tiles", tools["get_explored_map"].description)
        self.assertNotIn("cached for one second", tools["get_explored_map"].description)
        self.assertNotIn("placement-tick fallback", tools["get_entity_inventory"].description)


if __name__ == "__main__":
    unittest.main()
