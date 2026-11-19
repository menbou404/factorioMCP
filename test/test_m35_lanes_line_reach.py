import copy
import json
import unittest
from unittest.mock import Mock, patch

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import _normalize_result, create_server


def payload(response):
    return json.loads(response.content[0].text)


class LanesLineReachTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_lanes_and_inserter_drop_lane_single_batch_and_cached(self):
        cargo = [{"name": "coal", "quality": "normal", "count": 2}]
        for measured in (
            {
                "name": "transport-belt",
                "type": "transport-belt",
                "inventories": {},
                "belt_items": cargo,
                "belt_lanes": {"left": cargo, "right": {}},
            },
            {
                "name": "burner-inserter",
                "type": "inserter",
                "inventories": {},
                "drop_lane": "right",
            },
        ):
            expected = copy.deepcopy(measured)
            if "belt_lanes" in expected:
                expected["belt_lanes"]["right"] = []
            bridge = Mock()
            server = create_server(bridge)
            bridge.call.return_value = {"result": copy.deepcopy(measured)}
            single = payload(await server.call_tool("get_entity_inventory", {"target_ref": "a"}))
            for field, value in expected.items():
                self.assertEqual(single[field], value)
            batch = {
                "results": [{"index": 1, "ok": True, "result": copy.deepcopy(measured)}],
                "succeeded": 1,
                "failed": 0,
            }
            bridge.call.return_value = {"result": batch}
            result = payload(
                await server.call_tool("get_entity_inventories", {"target_refs": ["a"]})
            )
            self.assertEqual(result["results"][0]["result"], expected)
            cached = {"original_response": {"result": copy.deepcopy(measured)}}
            self.assertEqual(
                _normalize_result("get_request_result", cached)["original_response"]["result"],
                expected,
            )
        empty = _normalize_result(
            "get_entity_inventory", {"belt_items": {}, "belt_lanes": {"left": {}, "right": {}}}
        )
        self.assertEqual(empty, {"belt_items": [], "belt_lanes": {"left": [], "right": []}})
        self.assertNotIn("belt_lanes", _normalize_result("get_entity_inventory", {}))
        self.assertNotIn("drop_lane", _normalize_result("place_entity", {}))

    async def test_placement_drop_lane_single_and_batch(self):
        bridge = Mock()
        server = create_server(bridge)
        placement = {"item": "burner-inserter", "surface": "nauvis", "x": 0.5, "y": 0.5}
        for side in ("left", "right"):
            measured = {"name": "burner-inserter", "drop_lane": side}
            bridge.call.return_value = {"result": measured}
            self.assertEqual(
                payload(await server.call_tool("place_entity", placement))["drop_lane"], side
            )
            bridge.call.return_value = {
                "result": {
                    "results": [{"index": 1, "ok": True, "result": measured}],
                    "succeeded": 1,
                    "failed": 0,
                }
            }
            batch = payload(await server.call_tool("place_entities", {"items": [placement]}))
            self.assertEqual(batch["results"][0]["result"]["drop_lane"], side)

    async def test_line_stand_position_and_resume_hint(self):
        bridge = Mock()
        measured = {
            "complete": False,
            "placed": 4,
            "stopped": "out_of_range",
            "next_position": {"x": 10.5, "y": 0.5},
            "stand_position": {"x": 15.5, "y": 2.85},
        }
        bridge.call.return_value = {"result": measured}
        value = payload(
            await create_server(bridge).call_tool(
                "place_line",
                {
                    "item": "transport-belt",
                    "surface": "nauvis",
                    "from_x": 6.5,
                    "from_y": 0.5,
                    "to_x": 20.5,
                    "to_y": 0.5,
                    "end_direction": 0,
                },
            )
        )
        self.assertEqual(value["stand_position"], measured["stand_position"])
        self.assertEqual(value["next_position"], measured["next_position"])
        for text in ("move_to", "stand_position", "next_position", "same end_direction"):
            self.assertIn(text, value["hint"])
        del measured["stand_position"]
        self.assertNotIn("stand_position", _normalize_result("place_line", measured))

    async def assert_full_output(self, full_at, produced, diagnostics_fail):
        elapsed = 0.0

        async def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        def read(operation, _):
            if operation == "get_character_position":
                return {"result": {"position": {"surface": "nauvis", "x": 1, "y": 1}}}
            if operation == "entity_feeders":
                if diagnostics_fail:
                    raise BridgeError("target_gone", "Optional diagnostics failed")
                return {"result": {"feeders": [{"name": "inserter", "status": "no_power"}]}}
            self.assertEqual(operation, "get_entity_inventory")
            return {
                "result": {
                    "name": "assembling-machine-1",
                    "type": "assembling-machine",
                    "status": "full_output" if elapsed >= full_at else "working",
                    "inventories": {"result": [{"name": "iron-plate", "count": 3}]},
                    "products_finished": 40,
                    "recipe_products": ["iron-plate"],
                    "recipe_product_amounts": {"iron-plate": 1},
                }
            }

        bridge = Mock()
        bridge.call.side_effect = read
        with (
            patch("factorio_mcp.server.time") as clock,
            patch("factorio_mcp.server.asyncio.sleep", side_effect=advance) as sleep,
        ):
            clock.monotonic.side_effect = lambda: elapsed
            response = await create_server(bridge).call_tool(
                "wait_for_items",
                {"target_ref": "a", "item": "iron-plate", "count": 6, "produced": produced},
            )
        self.assertFalse(response.is_error)
        value = payload(response)
        self.assertEqual(elapsed, full_at + 3)
        self.assertEqual(sleep.await_count, (full_at + 3) * 2)
        self.assertTrue(value["output_full"])
        self.assertTrue(value["stalled"])
        self.assertFalse(value["timed_out"])
        self.assertFalse(value["reached"])
        self.assertEqual(value["current"], 0 if produced else 3)
        self.assertEqual(value["status"], "full_output")
        self.assertEqual(value["stalled_seconds"], 3)
        self.assertEqual(value["waited_seconds"], full_at + 3)
        self.assertEqual("feeders" in value, not diagnostics_fail)
        for text in ("current count", "only after", "taken out", "output inserter"):
            self.assertIn(text, value["hint"])

    async def test_full_output_after_three_seconds_and_on_later_poll_preserves_diagnostics(self):
        for full_at, produced, diagnostics_fail in (
            (0, False, False),
            (1.5, False, True),
            (0, True, False),
        ):
            with self.subTest(full_at=full_at, produced=produced):
                await self.assert_full_output(full_at, produced, diagnostics_fail)

    async def test_descriptions_explain_lanes_resume_and_three_second_full_output(self):
        tools = {tool.name: tool for tool in await create_server(Mock()).list_tools()}
        for name in ("get_entity_inventory", "get_entity_inventories"):
            for text in (
                "belt_lanes",
                "direction of travel",
                "Underground",
                "splitters",
                "drop_lane",
                "two item types",
            ):
                self.assertIn(text, tools[name].description)
        for name in ("place_entity", "place_entities"):
            for text in ("drop_lane", "opposite lanes", "ahead", "behind"):
                self.assertIn(text, tools[name].description)
        self.assertIn("only belt_items", tools["observe_nearby"].description)
        for text in ("stand_position", "move_to", "next_position", "same end_direction", "omitted"):
            self.assertIn(text, tools["place_line"].description)
        for text in (
            "3 consecutive seconds of full_output",
            "output_full=true",
            "during the wait",
            "current count",
            "output inserter",
        ):
            self.assertIn(text, tools["wait_for_items"].description)
