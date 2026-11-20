import copy
import json
import unittest
from unittest.mock import Mock, patch

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import create_server


def payload(response):
    return json.loads(response.content[0].text)


BELT_TYPES = ("transport-belt", "underground-belt", "splitter")
WAIT = {"target_ref": "belt", "item": "coal", "count": 3, "timeout_seconds": 1}
MISSING = [{"name": "copper-plate", "count": 1}]


class LineApproachBeltWaitTests(unittest.IsolatedAsyncioTestCase):
    async def belt_wait(self, kind, counts, extra=None):
        elapsed = 0.0
        reads = 0

        async def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        def respond(operation, params):
            nonlocal reads
            if operation == "get_character_position":
                return {"result": {"position": {"surface": "nauvis", "x": 0, "y": 0}}}
            self.assertEqual(operation, "get_entity_inventory")
            self.assertEqual(params, {"target_ref": "belt"})
            count = counts[min(reads, len(counts) - 1)]
            reads += 1
            return {
                "result": {
                    "name": kind,
                    "type": kind,
                    "inventories": {},
                    "belt_items": [
                        {"name": "coal", "quality": "normal", "count": count},
                        {"name": "coal", "quality": "uncommon", "count": 1},
                        {"name": "iron-plate", "quality": "normal", "count": 20},
                    ],
                }
            }

        bridge = Mock()
        bridge.call.side_effect = respond
        with (
            patch("factorio_mcp.server.time") as clock,
            patch("factorio_mcp.server.asyncio.sleep", side_effect=advance),
        ):
            clock.monotonic.side_effect = lambda: elapsed
            response = await create_server(bridge).call_tool(
                "wait_for_items", {**WAIT, **(extra or {})}
            )
        return response, reads

    async def test_all_belt_types_count_current_contents_across_qualities(self):
        for kind in BELT_TYPES:
            with self.subTest(kind=kind):
                response, reads = await self.belt_wait(kind, [2])
                self.assertFalse(response.is_error, payload(response))
                result = payload(response)
                self.assertEqual(result["current"], 3)
                self.assertTrue(result["belt"])
                self.assertTrue(result["reached"])
                self.assertFalse(result["timed_out"])
                self.assertEqual(reads, 1)
                self.assertNotIn("produced", result)
                self.assertNotIn("inventory_role", result)

    async def test_belt_recounts_moving_items_until_reached_or_timeout(self):
        for counts, reached, current in (([0, 2], True, 3), ([1, 0], False, 1)):
            response, reads = await self.belt_wait("transport-belt", counts)
            self.assertFalse(response.is_error, payload(response))
            result = payload(response)
            self.assertTrue(result["belt"])
            self.assertEqual(result["current"], current)
            self.assertEqual(result["reached"], reached)
            self.assertEqual(result["timed_out"], not reached)
            self.assertFalse(result["stalled"])
            self.assertGreaterEqual(reads, 2)
            self.assertNotIn("feeders", result)

    async def test_belt_rejects_produced_and_inventory_roles_server_side(self):
        for kind in BELT_TYPES:
            for extra in ({"produced": True}, {"inventory_role": "main"}):
                with self.subTest(kind=kind, extra=extra):
                    response, reads = await self.belt_wait(kind, [2], extra)
                    self.assertTrue(response.is_error)
                    self.assertEqual(payload(response)["error"], "invalid_input")
                    self.assertEqual(reads, 1)

    async def test_inventory_read_errors_preserve_mod_error_and_detail(self):
        for code in ("invalid_target", "out_of_range", "target_not_found"):
            bridge = Mock()

            def respond(operation, params, code=code):
                if operation == "get_character_position":
                    return {"result": {"position": {"x": 0, "y": 0}}}
                raise BridgeError(code, "fixture read failed", {"reason": "fixture"})

            bridge.call.side_effect = respond
            response = await create_server(bridge).call_tool("wait_for_items", WAIT)
            self.assertTrue(response.is_error)
            self.assertEqual(payload(response)["error"], code)
            self.assertEqual(payload(response)["detail"], {"reason": "fixture"})

    async def test_destination_status_missing_ingredients_fluids_and_hint_passthrough(self):
        bridge = Mock()
        server = create_server(bridge)
        for status in (
            "no_recipe",
            "full_output",
            "item_ingredient_shortage",
            "fluid_ingredient_shortage",
        ):
            drop = {"target_ref": "assembler", "name": "assembling-machine-2", "status": status}
            if status == "item_ingredient_shortage":
                drop["missing_ingredients"] = MISSING
            if status == "fluid_ingredient_shortage":
                drop["missing_fluids"] = [{"name": "water", "amount": 10}]
            record = {
                "name": "inserter",
                "type": "inserter",
                "inventories": {},
                "status": "waiting_for_space_in_destination",
                "drop_target": drop,
                "hint": (
                    "Check drop_target.status and drop_target.missing_ingredients or "
                    "drop_target.missing_fluids."
                ),
            }
            for name, args, value in (
                ("get_entity_inventory", {"target_ref": "inserter"}, record),
                (
                    "get_entity_inventories",
                    {"target_refs": ["inserter"]},
                    {"results": [{"index": 1, "ok": True, "result": record}]},
                ),
            ):
                with self.subTest(status=status, name=name):
                    bridge.call.return_value = {"result": copy.deepcopy(value)}
                    response = await server.call_tool(name, args)
                    self.assertFalse(response.is_error, payload(response))
                    actual = payload(response)
                    if name == "get_entity_inventories":
                        actual = actual["results"][0]["result"]
                    self.assertEqual(actual["drop_target"], drop)
                    self.assertEqual(actual["hint"], record["hint"])

    async def test_line_approach_terminal_result_passthrough(self):
        for complete in (False, True):
            measured = {
                "action_id": "line",
                "type": "place_line",
                "status": "succeeded",
                "placed": 8 if complete else 0,
                "complete": complete,
            }
            if not complete:
                measured.update(
                    stopped="out_of_range", detail={"reason": "stand_position_not_found"}
                )
            bridge = Mock()
            bridge.call.return_value = {"result": copy.deepcopy(measured)}
            result = payload(
                await create_server(bridge).call_tool("get_action_status", {"action_id": "line"})
            )
            for key, value in measured.items():
                self.assertEqual(result[key], value)

    async def test_descriptions_explain_belt_counts_and_stopped_inserters(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for phrase in (
            *BELT_TYPES,
            "all of the entity's transport lines",
            "belt=true",
            "number changes as items move",
            "inventory_role and produced=true on belts return invalid_input",
        ):
            self.assertIn(phrase, tools["wait_for_items"].description)
        for name in ("get_entity_inventory", "get_entity_inventories"):
            for phrase in (
                "drop_target.status",
                "missing_ingredients",
                "drop_target.missing_fluids",
                "no_recipe",
                "held item",
            ):
                self.assertIn(phrase, tools[name].description)
        for phrase in ("no stand candidate is visible", "128 tiles", "each index only once"):
            self.assertIn(phrase, tools["place_line"].description)


if __name__ == "__main__":
    unittest.main()
