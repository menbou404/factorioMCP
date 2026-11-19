import copy
import json
import unittest
from unittest.mock import Mock

from mcp.server.mcpserver.exceptions import ToolError

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import _normalize_result, create_server


def payload(response):
    return json.loads(response.content[0].text)


class BeltGoalBatchMineTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_and_strict_validation(self):
        bridge = Mock()
        server = create_server(bridge)
        tools = {tool.name: tool for tool in await server.list_tools()}
        schema = tools["mine_entities"].input_schema
        refs = schema["properties"]["target_refs"]
        self.assertEqual(schema["required"], ["target_refs"])
        self.assertEqual((refs["minItems"], refs["maxItems"]), (1, 32))
        self.assertTrue(refs["uniqueItems"])
        self.assertEqual(refs["items"]["type"], "string")
        self.assertEqual(
            schema["properties"]["wait_seconds"],
            tools["mine_entity"].input_schema["properties"]["wait_seconds"],
        )
        for value in ([], ["a"] * 33, ["a", "a"], [1], [True], [None], [""], "a"):
            with self.subTest(value=value), self.assertRaises(ToolError):
                await server.call_tool("mine_entities", {"target_refs": value})
        for seconds in (0, 51, float("nan"), float("inf")):
            with self.subTest(seconds=seconds), self.assertRaises(ToolError):
                await server.call_tool(
                    "mine_entities", {"target_refs": ["a"], "wait_seconds": seconds}
                )
        bridge.call.assert_not_called()
        bridge.call.return_value = {"result": {"action_id": "m", "status": "running"}}
        for refs in (["a"], [str(i) for i in range(32)]):
            await server.call_tool("mine_entities", {"target_refs": refs})
            bridge.call.assert_called_with("mine_entities", {"target_refs": refs})

    async def test_progress_stop_cancel_and_wait_passthrough(self):
        bridge = Mock()
        server = create_server(bridge)
        for status in ("running", "succeeded", "failed", "cancelled"):
            progress = {
                "mined": 2,
                "requested": 3,
                "current_index": 3,
                "gained": [{"name": "transport-belt", "quality": "normal", "count": 2}],
            }
            if status in ("failed", "cancelled"):
                progress["stopped"] = {
                    "index": 3,
                    "target_ref": "c",
                    "reason": "out_of_range" if status == "failed" else "cancelled",
                    "detail": {"distance": 12, "reach": 10},
                }
            value = {
                "action_id": "m",
                "type": "mine_entities",
                "status": status,
                "detail": copy.deepcopy(progress),
                **progress,
            }
            for tool, args in (
                ("mine_entities", {"target_refs": ["a", "b", "c"]}),
                ("get_action_status", {"action_id": "m"}),
                ("cancel_action", {"action_id": "m"}),
                ("wait_for_action", {"action_id": "m", "timeout_seconds": 1}),
                (
                    "mine_entities",
                    {"target_refs": ["a", "b", "c"], "wait_seconds": 1},
                ),
            ):
                bridge.call.return_value = {"result": copy.deepcopy(value)}
                with self.subTest(status=status, tool=tool):
                    result = payload(await server.call_tool(tool, args))
                    for field, expected in value.items():
                        self.assertEqual(result[field], expected)
        empty = {"type": "mine_entities", "gained": {}, "detail": {"gained": {}}}
        self.assertEqual(
            _normalize_result("get_action_status", empty),
            {"type": "mine_entities", "gained": [], "detail": {"gained": []}},
        )
        self.assertEqual(_normalize_result("mine_entity", {"gained": 2}), {"gained": 2})

    async def test_automatic_walk_wait_and_mine_conflict(self):
        for kind in ("move", "mine"):
            bridge = Mock()
            error = BridgeError(
                "action_conflict", "busy", {"running_action": {"id": "w", "type": kind}}
            )
            bridge.call.side_effect = [
                error,
                {"result": {"action_id": "w", "type": "move", "status": "succeeded"}},
                {"result": {"action_id": "m", "status": "running"}},
            ]
            result = payload(
                await create_server(bridge).call_tool("mine_entities", {"target_refs": ["a"]})
            )
            if kind == "move":
                self.assertEqual(result["action_id"], "m")
                self.assertEqual(result["waited_for_walk"]["status"], "succeeded")
                self.assertEqual(bridge.call.call_count, 3)
            else:
                self.assertEqual(result["error"], "action_conflict")
                self.assertEqual(bridge.call.call_count, 1)

    async def test_resource_validation_detail_passthrough(self):
        bridge = Mock()
        detail = {"errors": [{"index": 2, "reason": "resource_not_allowed"}]}
        bridge.call.side_effect = BridgeError("invalid_input", "resource", detail)
        response = await create_server(bridge).call_tool("mine_entities", {"target_refs": ["ore"]})
        self.assertTrue(response.is_error)
        self.assertEqual(payload(response)["detail"], detail)

    async def test_fluids_power_single_and_batch_passthrough(self):
        bridge = Mock()
        server = create_server(bridge)
        for power in (0, 900):
            value = {
                "name": "steam-engine",
                "type": "generator",
                "inventories": {},
                "fluids": [
                    {
                        "index": 1,
                        "name": "steam",
                        "amount": 100,
                        "capacity": 200,
                        "temperature": 165,
                    },
                    {"index": 2, "amount": 0, "capacity": 200},
                ],
                "power_output_kw": power,
            }
            bridge.call.return_value = {"result": copy.deepcopy(value)}
            measured = payload(await server.call_tool("get_entity_inventory", {"target_ref": "g"}))
            for field, expected in value.items():
                self.assertEqual(measured[field], expected)
            batch = {
                "results": [{"index": 1, "ok": True, "result": copy.deepcopy(value)}],
                "succeeded": 1,
                "failed": 0,
            }
            bridge.call.return_value = {"result": batch}
            result = payload(
                await server.call_tool("get_entity_inventories", {"target_refs": ["g"]})
            )
            self.assertEqual(result["results"][0]["result"], value)

    async def test_descriptions(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for phrase in ("within 3 tiles", "non-belt", "quarter-tile", "goal_adjusted", "radius"):
            self.assertIn(phrase, tools["move_to"].description)
        for phrase in (
            "1-32 unique",
            "without walking",
            "Resources",
            "detail.errors",
            "mine_entities the old tiles",
            "place_line",
            "status=failed",
            "no skips",
            "current_index",
            "cancel_action",
            "15 seconds",
        ):
            self.assertIn(phrase, tools["mine_entities"].description)
        for tool in ("get_entity_inventory", "get_entity_inventories"):
            for phrase in (
                "boiler",
                "steam engine",
                "water",
                "steam",
                "fuel",
                "fluids",
                "power_output_kw",
                "energy_generated_last_tick",
                "idle/no-steam is 0",
                "offshore-pump",
                "pipe-to-ground",
                "storage-tank",
            ):
                self.assertIn(phrase, tools[tool].description)


if __name__ == "__main__":
    unittest.main()
