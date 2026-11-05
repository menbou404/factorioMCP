import copy
import json
import unittest
from unittest.mock import Mock

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import HINTS, create_server


def payload(result):
    return json.loads(result.content[0].text)


class GroundItemTests(unittest.IsolatedAsyncioTestCase):
    arguments = {"item": "wooden-chest", "surface": "nauvis", "x": 2.5, "y": 3.5}
    stacks = [{"name": "iron-plate", "quality": "normal", "count": 3}]

    async def test_descriptions_and_blocked_hint(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in ("place_entity", "place_entities", "check_placement"):
            with self.subTest(name=name):
                for text in (
                    "Items on the ground inside the footprint",
                    "picked up into the main inventory",
                    "only blockers and all fit",
                    "ground_items_inventory_full",
                    "ground_items",
                    "free inventory space",
                    "stack={name, quality, count}",
                ):
                    self.assertIn(text, tools[name].description)
        for name in ("place_entity", "place_entities"):
            self.assertIn("collected_items", tools[name].description)
            self.assertIn("same tick", tools[name].description)
            self.assertIn("nothing is collected", tools[name].description)
        self.assertIn("This check collects nothing", tools["check_placement"].description)
        self.assertIn("placeable=true", tools["check_placement"].description)
        self.assertIn("placeable=false", tools["check_placement"].description)
        self.assertIn("ground_items_inventory_full", HINTS["blocked"])
        self.assertIn("free inventory space", HINTS["blocked"])

    async def test_preview_and_placement_normalize_present_arrays_only(self):
        bridge = Mock()
        server = create_server(bridge)
        for operation, field in (
            ("check_placement", "ground_items"),
            ("place_entity", "collected_items"),
        ):
            for stacks in ({}, [], None, self.stacks):
                with self.subTest(operation=operation, stacks=stacks):
                    measured = {field: copy.deepcopy(stacks), "placeable": True}
                    bridge.call.return_value = {"result": measured}
                    result = await server.call_tool(operation, self.arguments)
                    self.assertFalse(result.is_error)
                    self.assertEqual(payload(result)[field], stacks or [])
            bridge.call.return_value = {"result": {"placeable": True}}
            value = payload(await server.call_tool(operation, self.arguments))
            self.assertNotIn("ground_items", value)
            self.assertNotIn("collected_items", value)

    async def test_full_inventory_preview_keeps_measured_reason_and_stacks(self):
        measured = {
            "placeable": False,
            "reason": "blocked",
            "blocked_reason": "ground_items_inventory_full",
            "ground_items": self.stacks,
            "blocking": [
                {"name": "item-on-ground", "type": "item-entity", "stack": self.stacks[0]}
            ],
        }
        bridge = Mock()
        bridge.call.return_value = {"result": copy.deepcopy(measured)}
        result = await create_server(bridge).call_tool("check_placement", self.arguments)
        self.assertFalse(result.is_error)
        self.assertEqual(payload(result), {"ok": True, "tick": None, **measured})

    async def test_errors_and_batches_keep_and_normalize_stacks(self):
        bridge = Mock()
        server = create_server(bridge)
        for stacks in ({}, [], None, self.stacks):
            with self.subTest(stacks=stacks):
                detail = {
                    "reason": "ground_items_inventory_full",
                    "ground_items": copy.deepcopy(stacks),
                    "collected_items": copy.deepcopy(stacks),
                    "blocking": [
                        {"name": "item-on-ground", "type": "item-entity", "stack": self.stacks[0]}
                    ],
                }
                expected = {**detail, "ground_items": stacks or [], "collected_items": stacks or []}
                bridge.call.side_effect = BridgeError("blocked", detail=copy.deepcopy(detail))
                result = await server.call_tool("place_entity", self.arguments)
                self.assertTrue(result.is_error)
                self.assertEqual(payload(result)["detail"], expected)
                self.assertEqual(payload(result)["hint"], HINTS["blocked"])

                bridge.call.side_effect = None
                batch = {
                    "operation": "place_entity",
                    "results": [
                        {"index": 1, "ok": True, "result": {"collected_items": stacks}},
                        {"index": 2, "ok": False, "error": "blocked", "detail": detail},
                    ],
                    "succeeded": 1,
                    "failed": 1,
                }
                bridge.call.return_value = {"result": copy.deepcopy(batch)}
                result = await server.call_tool(
                    "place_entities", {"items": [self.arguments, self.arguments]}
                )
                self.assertFalse(result.is_error)
                entries = payload(result)["results"]
                self.assertEqual(entries[0]["result"]["collected_items"], stacks or [])
                self.assertEqual(entries[1]["detail"], expected)
                self.assertEqual(entries[1]["hint"], HINTS["blocked"])

                bridge.call.return_value = {"result": {"original_response": {"result": batch}}}
                cached = payload(
                    await server.call_tool("get_request_result", {"request_id": "ground-batch"})
                )
                self.assertEqual(cached["original_response"]["result"]["results"], entries)

    async def test_cached_single_responses_normalize_ground_item_arrays(self):
        bridge = Mock()
        server = create_server(bridge)
        for key in ("result", "detail"):
            bridge.call.return_value = {
                "result": {"original_response": {key: {"ground_items": {}, "collected_items": {}}}}
            }
            cached = payload(await server.call_tool("get_request_result", {"request_id": "ground"}))
            self.assertEqual(
                cached["original_response"][key], {"ground_items": [], "collected_items": []}
            )


if __name__ == "__main__":
    unittest.main()
