import json
import unittest
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

from factorio_mcp.bridge import BridgeError, FactorioBridge, _send_rcon, parse_bridge_response
from factorio_mcp.server import _normalize_result, create_server


class BridgeTests(unittest.TestCase):
    def test_send_rcon_disables_fragment_probe_for_large_factorio_responses(self):
        response = "x" * 4505
        with patch("factorio_mcp.bridge.Client") as client_type:
            client = client_type.return_value.__enter__.return_value
            client.run.return_value = response
            self.assertEqual(_send_rcon("127.0.0.1", 27277, "test", "/ai_mcp {}"), response)
            client_type.assert_called_once_with(
                "127.0.0.1", 27277, passwd="test", timeout=5, frag_threshold=1 << 30
            )
            client.run.assert_called_once_with("/ai_mcp {}")

    def test_matching_response_only(self):
        raw = "\n".join(
            [
                "unrelated log line",
                json.dumps({"protocol_version": 1, "request_id": "other", "ok": True}),
                json.dumps(
                    {"protocol_version": 1, "request_id": "wanted", "ok": True, "result": {"x": 2}}
                ),
            ]
        )
        self.assertEqual(parse_bridge_response(raw, "wanted")["result"], {"x": 2})
        with self.assertRaises(BridgeError) as caught:
            parse_bridge_response(raw, "missing")
        self.assertEqual(caught.exception.code, "mod_unavailable")

    def test_game_error_and_remote_host(self):
        raw = json.dumps(
            {"protocol_version": 1, "request_id": "x", "ok": False, "error": "missing_materials"}
        )
        with self.assertRaises(BridgeError) as caught:
            parse_bridge_response(raw, "x")
        self.assertEqual(caught.exception.code, "missing_materials")
        with self.assertRaises(BridgeError) as caught:
            FactorioBridge(host="192.0.2.1", password="test")
        self.assertEqual(caught.exception.code, "invalid_config")

    def test_uncertain_send_retries_same_request_id(self):
        request_ids = []

        def send(host, port, password, command):
            request = json.loads(command.removeprefix("/ai_mcp "))
            request_ids.append(request["request_id"])
            if len(request_ids) == 1:
                raise OSError("lost response")
            return json.dumps(
                {
                    "protocol_version": 1,
                    "request_id": request["request_id"],
                    "ok": True,
                    "result": {"action_id": "1"},
                }
            )

        bridge = FactorioBridge(password="test", send=send)
        self.assertEqual(bridge.call("move_to", {"x": 1})["result"]["action_id"], "1")
        self.assertEqual(request_ids, [request_ids[0], request_ids[0]])

    def test_failed_send_exposes_request_id(self):
        def send(*_):
            raise TimeoutError("lost response")

        bridge = FactorioBridge(password="test", send=send)
        with self.assertRaises(BridgeError) as caught:
            bridge.call("craft", {"recipe": "wooden-chest", "count": 1})
        self.assertEqual(caught.exception.code, "not_connected")
        self.assertEqual(len(caught.exception.detail["request_id"]), 36)


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_and_success_shape(self):
        def send(host, port, password, command):
            request = json.loads(command.removeprefix("/ai_mcp "))
            self.assertEqual(request["agent_id"], "agent-1")
            return json.dumps(
                {
                    "protocol_version": 1,
                    "request_id": request["request_id"],
                    "ok": True,
                    "tick": 42,
                    "result": {"items": {}},
                }
            )

        server = create_server(FactorioBridge(password="test", send=send))
        self.assertEqual(len(await server.list_tools()), 20)
        result = await server.call_tool("get_inventory", {})
        self.assertFalse(result.is_error)
        self.assertEqual(json.loads(result.content[0].text), {"ok": True, "tick": 42, "items": []})

    async def test_input_validation_and_unconfigured_error(self):
        server = create_server(None)
        result = await server.call_tool("factorio_ping", {})
        self.assertTrue(result.is_error)
        self.assertEqual(json.loads(result.content[0].text)["error"], "not_configured")
        move = next(tool for tool in await server.list_tools() if tool.name == "move_to")
        self.assertEqual(set(move.input_schema["required"]), {"surface", "x", "y"})
        place = next(tool for tool in await server.list_tools() if tool.name == "place_entity")
        self.assertEqual(place.input_schema["properties"]["direction"]["default"], 0)

    async def test_logistics_tool_schemas(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        check = tools["check_placement"].input_schema
        self.assertEqual(set(check["required"]), {"item", "surface", "x", "y"})
        self.assertEqual(check["properties"]["direction"]["enum"], [0, 4, 8, 12])
        self.assertEqual(check["properties"]["direction"]["default"], 0)
        rotate = tools["rotate_entity"].input_schema
        self.assertEqual(rotate["required"], ["target_ref"])
        self.assertEqual(rotate["properties"]["reverse"]["type"], "boolean")
        self.assertIs(rotate["properties"]["reverse"]["default"], False)

    async def test_power_research_tool_schemas(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name, fields in [
            ("set_recipe", {"target_ref", "recipe"}),
            ("start_research", {"technology"}),
            ("get_recipe_info", {"recipe"}),
        ]:
            with self.subTest(tool=name):
                schema = tools[name].input_schema
                self.assertEqual(set(schema["required"]), fields)
                self.assertEqual(set(schema["properties"]), fields)
                for field in fields:
                    self.assertEqual(schema["properties"][field]["type"], "string")
                    self.assertEqual(schema["properties"][field]["minLength"], 1)
                with self.assertRaises(ToolError):
                    await create_server(None).call_tool(name, dict.fromkeys(fields, ""))

    def test_observation_array_normalization(self):
        ore = {"name": "iron-ore", "tiles": 4, "total_amount": 1200}
        belt_item = {"name": "iron-ore", "quality": "normal", "count": 2}
        result = _normalize_result(
            "observe_nearby",
            {
                "resources": {},
                "entities": [
                    {"name": "transport-belt", "belt_items": {}},
                    {"name": "stone-furnace"},
                    {"name": "transport-belt", "belt_items": [belt_item]},
                ],
            },
        )
        self.assertEqual(result["resources"], [])
        self.assertEqual(result["entities"][0]["belt_items"], [])
        self.assertNotIn("belt_items", result["entities"][1])
        self.assertEqual(result["entities"][2]["belt_items"], [belt_item])
        self.assertEqual(
            _normalize_result("observe_nearby", {"resources": [ore]})["resources"], [ore]
        )

    def test_power_research_array_normalization(self):
        fluid = {"name": "steam", "amount": 100}
        observed = _normalize_result(
            "observe_nearby",
            {"entities": [{"fluids": {}}, {"fluids": [fluid]}, {"name": "iron-ore"}]},
        )
        self.assertEqual(observed["entities"][0]["fluids"], [])
        self.assertEqual(observed["entities"][1]["fluids"], [fluid])
        self.assertNotIn("fluids", observed["entities"][2])
        available = _normalize_result(
            "get_research_status", {"available": [{"name": "steam-power", "ingredients": {}}]}
        )["available"]
        self.assertEqual(available, [{"name": "steam-power", "ingredients": []}])
        for operation, fields in [
            ("get_research_status", ("researched", "queue", "available")),
            ("get_recipe_info", ("ingredients", "products")),
            ("start_research", ("queue",)),
        ]:
            with self.subTest(operation=operation):
                self.assertEqual(_normalize_result(operation, {}), dict.fromkeys(fields, []))
                self.assertEqual(
                    _normalize_result(operation, dict.fromkeys(fields, {})),
                    dict.fromkeys(fields, []),
                )
                values = {
                    "researched": ["steam-power"],
                    "queue": ["automation"],
                    "available": [
                        {
                            "name": "automation",
                            "unit_count": 10,
                            "ingredients": [{"name": "automation-science-pack", "amount": 1}],
                        }
                    ],
                    "ingredients": [{"name": "iron-plate", "amount": 2}],
                    "products": [{"name": "iron-gear-wheel", "amount": 1}],
                }
                populated = {field: values[field] for field in fields}
                self.assertEqual(_normalize_result(operation, populated.copy()), populated)

    def test_research_trigger_metadata_preserved_during_normalization(self):
        trigger = {"type": "craft-item", "item": "lab", "count": 1}
        result = _normalize_result(
            "get_research_status",
            {
                "available": [
                    {
                        "name": "automation-science-pack",
                        "unit_count": 0,
                        "unit_time": 0,
                        "ingredients": {},
                        "trigger": trigger,
                    },
                    {
                        "name": "automation",
                        "unit_count": 10,
                        "unit_time": 10,
                        "ingredients": [{"name": "automation-science-pack", "amount": 1}],
                    },
                ]
            },
        )
        triggered, normal = result["available"]
        self.assertEqual(
            triggered,
            {
                "name": "automation-science-pack",
                "unit_count": 0,
                "unit_time": 0,
                "ingredients": [],
                "trigger": trigger,
            },
        )
        self.assertNotIn("trigger", normal)
        self.assertEqual(normal["ingredients"], [{"name": "automation-science-pack", "amount": 1}])

    async def test_tools_forward_params_and_results(self):
        cases = [
            (
                "check_placement",
                {"item": "transport-belt", "surface": "nauvis", "x": 1.5, "y": 2.5},
                {"direction": 0},
                {"placeable": False, "reason": "blocked", "has_item": True},
            ),
            (
                "check_placement",
                {
                    "item": "burner-mining-drill",
                    "surface": "nauvis",
                    "x": -12,
                    "y": 11,
                    "direction": 4,
                },
                {},
                {"placeable": True, "entity": "burner-mining-drill", "has_item": False},
            ),
            ("rotate_entity", {"target_ref": "42"}, {"reverse": False}, {"direction": 4}),
            (
                "rotate_entity",
                {"target_ref": "43", "reverse": True},
                {},
                {"direction": 12},
            ),
            (
                "set_recipe",
                {"target_ref": "44", "recipe": "iron-gear-wheel"},
                {},
                {
                    "name": "assembling-machine-1",
                    "recipe": "iron-gear-wheel",
                    "position": {"surface": "nauvis", "x": 1.5, "y": 2.5},
                    "returned_items": [{"name": "iron-plate", "quality": "normal", "count": 2}],
                },
            ),
            (
                "start_research",
                {"technology": "automation"},
                {},
                {"technology": "automation", "current": "automation", "queue": ["automation"]},
            ),
            (
                "get_recipe_info",
                {"recipe": "iron-gear-wheel"},
                {},
                {
                    "name": "iron-gear-wheel",
                    "enabled": True,
                    "category": "crafting",
                    "energy": 0.5,
                    "ingredients": [{"name": "iron-plate", "amount": 2}],
                    "products": [{"name": "iron-gear-wheel", "amount": 1}],
                    "hand_craftable": True,
                },
            ),
        ]
        for operation, arguments, defaults, measured in cases:
            with self.subTest(operation=operation, arguments=arguments):

                def send(
                    host,
                    port,
                    password,
                    command,
                    operation=operation,
                    defaults=defaults,
                    arguments=arguments,
                    measured=measured,
                ):
                    request = json.loads(command.removeprefix("/ai_mcp "))
                    self.assertEqual(request["operation"], operation)
                    self.assertEqual(request["params"], {**defaults, **arguments})
                    return json.dumps(
                        {
                            "protocol_version": 1,
                            "request_id": request["request_id"],
                            "ok": True,
                            "tick": 42,
                            "result": measured,
                        }
                    )

                server = create_server(FactorioBridge(password="test", send=send))
                result = await server.call_tool(operation, arguments)
                self.assertFalse(result.is_error)
                self.assertEqual(
                    json.loads(result.content[0].text), {"ok": True, "tick": 42, **measured}
                )


if __name__ == "__main__":
    unittest.main()
