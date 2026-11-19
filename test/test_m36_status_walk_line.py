import copy
import json
import unittest
from unittest.mock import Mock, patch

from mcp.server.mcpserver.exceptions import ToolError

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import create_server


def payload(response):
    return json.loads(response.content[0].text)


LINE = {
    "item": "transport-belt",
    "surface": "nauvis",
    "from_x": 1.5,
    "from_y": 0.5,
    "to_x": 40.5,
    "to_y": 0.5,
    "end_direction": 0,
}
MISSING = [{"name": "iron-plate", "count": 2}]
RUNNING = {
    "action_id": "line-1",
    "type": "place_line",
    "status": "running",
    "item": "transport-belt",
    "requested": 40,
    "placed": 8,
    "already_present": 0,
    "complete": False,
    "first_position": {"x": 1.5, "y": 0.5},
    "last_position": {"x": 8.5, "y": 0.5},
    "next_position": {"x": 9.5, "y": 0.5},
    "direction": 4,
    "end_direction": 0,
}
FINAL = {
    **{key: value for key, value in RUNNING.items() if key != "next_position"},
    "status": "succeeded",
    "placed": 40,
    "complete": True,
    "last_position": {"x": 40.5, "y": 0.5},
}


class StatusWalkLineTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_walk_only_on_place_line_and_move_wait_bounds(self):
        bridge = Mock()
        server = create_server(bridge)
        tools = {tool.name: tool for tool in await server.list_tools()}
        props = tools["place_line"].input_schema["properties"]
        self.assertEqual(props["walk"], {"default": False, "title": "Walk", "type": "boolean"})
        self.assertEqual(
            props["wait_seconds"],
            tools["move_to"].input_schema["properties"]["wait_seconds"],
        )
        self.assertNotIn("walk", tools["check_line"].input_schema["properties"])
        self.assertNotIn("wait_seconds", tools["check_line"].input_schema["properties"])
        for args in (
            {**LINE, "walk": "true"},
            {**LINE, "walk": 1},
            {**LINE, "walk": True, "wait_seconds": 0},
            {**LINE, "walk": True, "wait_seconds": 51},
            {**LINE, "walk": True, "wait_seconds": float("nan")},
        ):
            with self.assertRaises(ToolError):
                await server.call_tool("place_line", args)
        with self.assertRaises(ToolError):
            await server.call_tool("check_line", {**LINE, "walk": True})
        bridge.call.assert_not_called()

    async def test_false_keeps_synchronous_call_and_true_starts_action(self):
        bridge = Mock()
        server = create_server(bridge)
        bridge.call.return_value = {"result": {"placed": 5, "complete": False}}
        for extra in ({}, {"walk": False}, {"walk": False, "wait_seconds": None}):
            result = payload(await server.call_tool("place_line", {**LINE, **extra}))
            self.assertEqual(result["placed"], 5)
            bridge.call.assert_called_with("place_line", LINE)
        bridge.call.return_value = {"result": copy.deepcopy(RUNNING)}
        result = payload(await server.call_tool("place_line", {**LINE, "walk": True}))
        bridge.call.assert_called_with("place_line", {**LINE, "walk": True})
        for field, expected in RUNNING.items():
            self.assertEqual(result[field], expected)
        self.assertEqual(result["waited_seconds"], 0)

    async def test_wait_seconds_requires_walk_true(self):
        bridge = Mock()
        server = create_server(bridge)
        for extra in ({}, {"walk": False}):
            with self.subTest(extra=extra):
                response = await server.call_tool(
                    "place_line", {**LINE, **extra, "wait_seconds": 20}
                )
                self.assertTrue(response.is_error)
                result = payload(response)
                self.assertEqual(result["error"], "invalid_input")
                self.assertEqual(result["message"], "wait_seconds requires walk=true.")
                self.assertEqual(
                    result["detail"]["errors"],
                    [
                        {
                            "field": "wait_seconds",
                            "type": "value_error",
                            "message": "wait_seconds requires walk=true.",
                        }
                    ],
                )
        bridge.call.assert_not_called()

    async def timed_call(self, operation, args, respond):
        elapsed = 0.0

        async def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        bridge = Mock()
        bridge.call.side_effect = lambda name, params: respond(name, params, elapsed)
        with (
            patch("factorio_mcp.server.time") as clock,
            patch("factorio_mcp.server.asyncio.sleep", side_effect=advance),
        ):
            clock.monotonic.side_effect = lambda: elapsed
            response = await create_server(bridge).call_tool(operation, args)
        self.assertFalse(response.is_error, payload(response))
        return payload(response), elapsed, bridge

    async def test_walk_wait_final_and_timeout_progress_then_wait_for_action(self):
        for finishes in (True, False):

            def respond(name, params, elapsed, finishes=finishes):
                if name == "place_line":
                    self.assertEqual(params, {**LINE, "walk": True})
                    return {"result": copy.deepcopy(RUNNING)}
                self.assertEqual(name, "get_action_status")
                self.assertEqual(params, {"action_id": "line-1"})
                value = FINAL if finishes and elapsed >= 0.5 else {**RUNNING, "placed": 20}
                return {"result": copy.deepcopy(value)}

            result, elapsed, _ = await self.timed_call(
                "place_line", {**LINE, "walk": True, "wait_seconds": 1}, respond
            )
            self.assertEqual(elapsed, 0.5 if finishes else 1)
            self.assertEqual(result["timed_out"], not finishes)
            self.assertEqual(result["waited_seconds"], elapsed)
            for field, expected in (FINAL if finishes else {**RUNNING, "placed": 20}).items():
                self.assertEqual(result[field], expected)
            if finishes:
                # Initial running fields must not leak into a completed result.
                self.assertNotIn("next_position", result)
                self.assertNotIn("hint", result)

        bridge = Mock()
        server = create_server(bridge)
        for status, stopped in (("succeeded", "blocked"), ("cancelled", "cancelled")):
            measured = {
                **RUNNING,
                "status": status,
                "stopped": stopped,
                "detail": {"reason": "test-stop"},
                "collected_items": [{"name": "coal", "quality": "normal", "count": 1}],
            }
            for tool, args in (
                ("get_action_status", {"action_id": "line-1"}),
                ("wait_for_action", {"action_id": "line-1", "timeout_seconds": 1}),
                ("cancel_action", {"action_id": "line-1"}),
                ("place_line", {**LINE, "walk": True, "wait_seconds": 1}),
            ):
                bridge.call.return_value = {"result": copy.deepcopy(measured)}
                result = payload(await server.call_tool(tool, args))
                for field, expected in measured.items():
                    self.assertEqual(result[field], expected)

    async def test_walk_conflict_automatically_waits_before_starting_line(self):
        for running_type in ("move", "place_line"):
            starts = 0

            def respond(name, params, elapsed, running_type=running_type):
                nonlocal starts
                if name == "place_line":
                    starts += 1
                    if starts == 1:
                        raise BridgeError(
                            "action_conflict",
                            detail={"running_action": {"id": "old", "type": running_type}},
                        )
                    return {"result": copy.deepcopy(FINAL)}
                self.assertEqual(name, "get_action_status")
                return {"result": {"action_id": "old", "status": "succeeded"}}

            result, _, _ = await self.timed_call(
                "place_line", {**LINE, "walk": True, "wait_seconds": 20}, respond
            )
            self.assertEqual(starts, 2)
            self.assertEqual(result["waited_for_walk"]["action_id"], "old")
            self.assertTrue(result["complete"])

    async def test_corrected_inventory_observation_batch_and_transfer_passthrough(self):
        measured = {"status": "item_ingredient_shortage", "missing_ingredients": MISSING}
        bridge = Mock()
        server = create_server(bridge)
        for tool, args, data in (
            ("get_entity_inventory", {"target_ref": "a"}, {"inventories": {}, **measured}),
            ("observe_nearby", {}, {"entities": [{"name": "assembling-machine-1", **measured}]}),
            (
                "get_entity_inventories",
                {"target_refs": ["a"]},
                {"results": [{"index": 1, "ok": True, "result": measured}]},
            ),
        ):
            bridge.call.return_value = {"result": copy.deepcopy(data)}
            result = payload(await server.call_tool(tool, args))
            record = result
            if "entities" in result:
                record = result["entities"][0]
            if "results" in result:
                record = result["results"][0]["result"]
            self.assertEqual(record["status"], measured["status"])
            self.assertEqual(record["missing_ingredients"], MISSING)

        transfer = {
            "target_ref": "a",
            "direction": "from_entity",
            "inventory_role": "result",
            "item": "iron-gear-wheel",
            "count": 1,
        }
        bridge.call.side_effect = BridgeError("missing_materials", detail=copy.deepcopy(measured))
        result = payload(await server.call_tool("transfer_item", transfer))
        self.assertEqual(result["detail"], measured)
        bridge.call.side_effect = None
        bridge.call.return_value = {
            "result": {
                "results": [
                    {"index": 1, "ok": False, "error": "missing_materials", "detail": measured}
                ]
            }
        }
        result = payload(await server.call_tool("transfer_items", {"items": [transfer]}))
        self.assertEqual(result["results"][0]["detail"], measured)

    async def test_shortage_stalls_at_ten_seconds_full_output_at_three(self):
        for status, seconds in (("item_ingredient_shortage", 10), ("full_output", 3)):
            for produced in (False, True):

                def respond(name, params, elapsed, status=status):
                    if name == "get_character_position":
                        return {"result": {"position": {"surface": "nauvis", "x": 0, "y": 0}}}
                    if name == "entity_feeders":
                        return {"result": {"feeders": []}}
                    self.assertEqual(name, "get_entity_inventory")
                    result = {
                        "name": "assembling-machine-1",
                        "type": "assembling-machine",
                        "status": status,
                        "inventories": {"result": [{"name": "iron-gear-wheel", "count": 3}]},
                        "recipe_products": ["iron-gear-wheel"],
                        "recipe_product_amounts": {"iron-gear-wheel": 1},
                        "products_finished": 3,
                    }
                    if status == "item_ingredient_shortage":
                        result["missing_ingredients"] = MISSING
                    return {"result": result}

                result, elapsed, _ = await self.timed_call(
                    "wait_for_items",
                    {
                        "target_ref": "a",
                        "item": "iron-gear-wheel",
                        "count": 5,
                        "produced": produced,
                    },
                    respond,
                )
                self.assertEqual(elapsed, seconds)
                self.assertEqual(result["stalled_seconds"], seconds)
                self.assertTrue(result["stalled"])
                self.assertFalse(result["timed_out"])
                self.assertEqual(result["status"], status)
                self.assertEqual(result.get("output_full", False), status == "full_output")
                if status == "item_ingredient_shortage":
                    self.assertEqual(result["missing_ingredients"], MISSING)
                    self.assertIn("missing_ingredients", result["hint"])
                else:
                    self.assertNotIn("missing_ingredients", result)

    async def test_descriptions(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for tool in (
            "observe_nearby",
            "get_entity_inventory",
            "get_entity_inventories",
            "wait_for_items",
        ):
            description = tools[tool].description
            for text in (
                "about one stack per product",
                "missing_ingredients",
                "item_ingredient_shortage",
            ):
                self.assertIn(text, description)
            self.assertNotIn("recipe-dependent", description)
        for text in (
            "walk=true",
            "walk=false",
            "long line in one call",
            "stand_position",
            "wait_seconds",
            "wait_seconds requires walk=true",
            "wait_for_action",
            "next_position",
        ):
            self.assertIn(text, tools["place_line"].description)
