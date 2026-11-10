import copy
import json
import unittest
from unittest.mock import Mock, patch

from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import NO_GENERATION_HINT, _normalize_result, create_server


def payload(response):
    return json.loads(response.content[0].text)


class LimitWalkPowerTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_schema_and_descriptions(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        tool = tools["set_inventory_limit"]
        self.assertEqual(tool.input_schema["required"], ["target_ref"])
        self.assertEqual(tool.input_schema["properties"]["slots"]["default"], None)
        for text in (
            "container",
            "interact reach",
            "null",
            "0",
            "total slot count",
            "slots: 1 keeps one stack, e.g. 100 belts",
            "Inserters stop inserting",
            "items already there stay",
            "invalid_params",
            "Works while walking or mining, within interact reach",
        ):
            self.assertIn(text, tool.description)
        self.assertNotIn("action_conflict", tool.description)
        for name in ("observe_nearby", "get_entity_inventory", "get_entity_inventories"):
            self.assertIn("inventory_limit", tools[name].description)
        for name in ("move_to", "wait_for_action"):
            self.assertIn("remaining_distance", tools[name].description)
            self.assertIn("2 decimals", tools[name].description)
            self.assertIn("before placing or transferring", tools[name].description)
            self.assertNotIn("changing settings", tools[name].description)
        for tool in tools.values():
            if "electric_network_connected" in tool.description:
                for text in (
                    "belongs to an electric network",
                    "does not mean the network has generation",
                    "nearby electric pole",
                    "get_entity_inventory",
                    "production and consumption",
                ):
                    self.assertIn(text, tool.description)
        for name in ("place_entity", "place_entities"):
            self.assertIn("electric_network statistics", tools[name].description)

    async def test_python_validation_never_calls_bridge(self):
        for mode in (None, "auto", "legacy"):
            bridge = Mock()
            server = create_server(bridge)

            async def check(client, mode=mode):
                for value in (-1, 1.5, 1.0, True, False, "1", [], {}):
                    with self.subTest(mode=mode, value=value):
                        try:
                            response = await client.call_tool(
                                "set_inventory_limit", {"target_ref": "chest", "slots": value}
                            )
                            self.assertTrue(response.is_error)
                            result = payload(response)
                        except ToolError as exc:
                            result = json.loads(str(exc))
                        self.assertEqual(result["error"], "invalid_params")

            if mode is None:
                await check(server)
            else:
                async with Client(server, mode=mode) as client:
                    await check(client)
            bridge.call.assert_not_called()

    async def test_setter_arguments_results_and_mod_errors(self):
        bridge = Mock()
        server = create_server(bridge)
        for args in ({}, {"slots": None}, {"slots": 0}, {"slots": 1}, {"slots": 16}):
            result = {
                "name": "wooden-chest",
                "position": {"x": 1, "y": 2},
                "target_ref": "chest",
                "slots": 16,
            }
            if args.get("slots") in (0, 1):
                result["inventory_limit"] = args["slots"]
            bridge.call.return_value = {"tick": 8, "result": copy.deepcopy(result)}
            response = await server.call_tool(
                "set_inventory_limit", {"target_ref": "chest", **args}
            )
            self.assertEqual(
                payload(response),
                {"ok": True, "tick": 8, **result, "inventory_limit": result.get("inventory_limit")},
            )
            bridge.call.assert_called_with(
                "set_inventory_limit", {"target_ref": "chest", "slots": args.get("slots")}
            )
        for code, detail in (
            ("invalid_params", {"slots": 16}),
            ("invalid_target", {"reason": "wrong_entity_type", "entity": "inserter"}),
            ("out_of_range", {"distance": 20, "reach": 10}),
        ):
            bridge.call.side_effect = BridgeError(code, code, detail)
            response = await server.call_tool(
                "set_inventory_limit", {"target_ref": "chest", "slots": 99}
            )
            self.assertEqual(payload(response)["error"], code)
            self.assertEqual(payload(response)["detail"], detail)

    async def test_inventory_limit_read_batch_observation_and_cache_passthrough(self):
        for limit in (0, 1, None):
            measured = {"name": "wooden-chest", "type": "container", "inventories": {"main": []}}
            if limit is not None:
                measured["inventory_limit"] = limit
            self.assertEqual(
                _normalize_result("get_entity_inventory", copy.deepcopy(measured)), measured
            )
            batch = {"results": [{"index": 1, "ok": True, "result": copy.deepcopy(measured)}]}
            self.assertEqual(
                _normalize_result("get_entity_inventories", batch)["results"][0]["result"], measured
            )
            observed = {
                "filters": {"compact": True},
                "entities": [copy.deepcopy(measured)],
                "blocked_tiles": [],
                "resources": [],
            }
            self.assertEqual(_normalize_result("observe_nearby", copy.deepcopy(observed)), observed)
        cached = {
            "original_response": {
                "result": {
                    "name": "wooden-chest",
                    "slots": 16,
                    "position": {"x": 1, "y": 2},
                    "target_ref": "chest",
                }
            }
        }
        self.assertIsNone(
            _normalize_result("get_request_result", cached)["original_response"]["result"][
                "inventory_limit"
            ]
        )

    async def test_running_walk_distance_and_combined_hints(self):
        for operation, arguments in (
            ("move_to", {"surface": "nauvis", "x": 80, "y": 0}),
            ("move_to", {"surface": "nauvis", "x": 80, "y": 0, "wait_seconds": 1}),
            ("wait_for_action", {"action_id": "walk", "timeout_seconds": 1}),
        ):
            bridge = Mock(return_value=None)
            measured = {
                "action_id": "walk",
                "type": "move",
                "status": "running",
                "remaining_distance": 71.23,
                "hint": "Keep this existing hint.",
            }
            bridge.call.return_value = {"tick": 12, "result": copy.deepcopy(measured)}
            with patch("factorio_mcp.server.time") as clock:
                clock.monotonic.side_effect = [0, 2]
                response = await create_server(bridge).call_tool(operation, arguments)
            result = payload(response)
            self.assertEqual(result["remaining_distance"], 71.23)
            self.assertTrue(result["hint"].startswith(measured["hint"] + " "))
            for text in (
                "place_entity",
                "place_entities",
                "place_line",
                "transfer_item(s)",
                "action_conflict",
                "For longer walks, call wait_for_action with this action_id: walk",
                "automatically waits up to 15 seconds",
                "waited_for_walk",
                "walk",
            ):
                self.assertIn(text, result["hint"])
            for name in (
                "rotate_entity",
                "set_recipe",
                "set_inserter_filter",
                "set_splitter",
                "set_inventory_limit",
            ):
                self.assertNotIn(name, result["hint"])

    async def test_completed_walk_does_not_inherit_running_guidance(self):
        bridge = Mock()
        bridge.call.side_effect = [
            {
                "result": {
                    "action_id": "walk",
                    "type": "move",
                    "status": "running",
                    "remaining_distance": 8,
                }
            },
            {
                "result": {
                    "action_id": "walk",
                    "type": "move",
                    "status": "succeeded",
                    "hint": "Stand off belts.",
                }
            },
        ]
        result = payload(
            await create_server(bridge).call_tool(
                "move_to", {"surface": "nauvis", "x": 8, "y": 0, "wait_seconds": 1}
            )
        )
        self.assertEqual(result["hint"], "Stand off belts.")
        self.assertNotIn("remaining_distance", result)

    async def test_conflict_distance_and_hint_passthrough(self):
        bridge = Mock()
        detail = {"running_action": {"id": "walk", "type": "move", "remaining_distance": 51.21}}
        bridge.call.side_effect = BridgeError("action_conflict", "busy", copy.deepcopy(detail))
        result = payload(
            await create_server(bridge).call_tool(
                "place_entity", {"item": "wooden-chest", "surface": "nauvis", "x": 1, "y": 2}
            )
        )
        self.assertEqual(result["detail"], detail)
        self.assertIn("wait_for_action", result["hint"])

    async def test_pole_placement_network_batch_cache_and_combined_hint(self):
        bridge = Mock()
        server = create_server(bridge)
        for producers in ({}, [], [{"name": "electric-energy-interface", "kw": 10}]):
            measured = {
                "name": "small-electric-pole",
                "consumed_item": "small-electric-pole",
                "electric_network_connected": True,
                "hint": "Existing placement hint.",
                "electric_network": {
                    "id": 1,
                    "production_kw": 0,
                    "consumption_kw": 0,
                    "producers": producers,
                    "consumers": {},
                },
            }
            expected = copy.deepcopy(measured)
            expected["electric_network"].update(producers=producers or [], consumers=[])
            if not producers:
                expected["hint"] += " " + NO_GENERATION_HINT
            for operation, params, result in (
                (
                    "place_entity",
                    {"item": "small-electric-pole", "surface": "nauvis", "x": 1, "y": 2},
                    measured,
                ),
                (
                    "place_entities",
                    {
                        "items": [
                            {"item": "small-electric-pole", "surface": "nauvis", "x": 1, "y": 2}
                        ]
                    },
                    {"results": [{"index": 1, "ok": True, "result": copy.deepcopy(measured)}]},
                ),
                (
                    "get_request_result",
                    {"request_id": "old"},
                    {"original_response": {"result": copy.deepcopy(measured)}},
                ),
            ):
                bridge.call.return_value = {"tick": 10, "result": copy.deepcopy(result)}
                actual = payload(await server.call_tool(operation, params))
                if operation == "place_entities":
                    actual = actual["results"][0]["result"]
                elif operation == "get_request_result":
                    actual = actual["original_response"]["result"]
                else:
                    actual = {k: v for k, v in actual.items() if k not in ("ok", "tick")}
                self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
