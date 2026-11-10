import json
import unittest
from unittest.mock import Mock

from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import HINTS, INSERTER_DIRECTION_DESCRIPTION, create_server


def payload(response):
    return json.loads(response.content[0].text)


class StrictArgsRotateTests(unittest.IsolatedAsyncioTestCase):
    async def invalid(self, server, name, arguments):
        try:
            response = await server.call_tool(name, arguments)
        except ToolError as exc:
            value = json.loads(str(exc))
        else:
            self.assertTrue(response.is_error)
            value = payload(response)
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"], "invalid_input")
        return value

    async def test_unknown_arguments_on_every_registered_tool_never_call_bridge(self):
        bridge = Mock()
        server = create_server(bridge)
        for tool in await server.list_tools():
            with self.subTest(tool=tool.name):
                # Missing required fields must be collected alongside the extra.
                value = await self.invalid(server, tool.name, {"typo": 1})
                unknown = [
                    e for e in value["detail"]["errors"] if e["type"] == "unexpected_argument"
                ]
                self.assertEqual([e["field"] for e in unknown], ["typo"])
                accepted = sorted(tool.input_schema.get("properties", {}))
                self.assertIn(
                    "Accepted arguments: " + (", ".join(accepted) or "(none)") + ".",
                    value["hint"],
                )
        bridge.call.assert_not_called()

    async def test_valid_arguments_with_extras_and_mixed_errors_in_both_client_modes(self):
        cases = [
            (
                "factorio_ping",
                {"z": 1, "a": 2},
                {"a": "unexpected_argument", "z": "unexpected_argument"},
            ),
            (
                "rotate_entity",
                {"target_ref": "inserter", "dir": 12},
                {"dir": "unexpected_argument"},
            ),
            ("observe_nearby", {"raduis": 10}, {"raduis": "unexpected_argument"}),
            (
                "mine_entity",
                {"target_ref": "ore", "count": 101, "z": 1, "a": 2},
                {
                    "a": "unexpected_argument",
                    "z": "unexpected_argument",
                    "count": "less_than_equal",
                },
            ),
            (
                "set_inventory_limit",
                {"target_ref": "chest", "slots": -1, "extra": 0},
                {"extra": "unexpected_argument", "slots": "greater_than_equal"},
            ),
        ]
        for mode in ("auto", "legacy"):
            bridge = Mock()
            async with Client(create_server(bridge), mode=mode) as client:
                for name, arguments, expected in cases:
                    with self.subTest(mode=mode, tool=name):
                        response = await client.call_tool(name, arguments)
                        self.assertTrue(response.is_error)
                        value = payload(response)
                        self.assertEqual(value["error"], "invalid_input")
                        errors = value["detail"]["errors"]
                        self.assertEqual({e["field"]: e["type"] for e in errors}, expected)
                        unknown = [e["field"] for e in errors if e["type"] == "unexpected_argument"]
                        self.assertEqual(unknown, sorted(unknown))
                        for error in errors:
                            self.assertEqual(set(error), {"field", "type", "message"})
                            self.assertTrue(error["message"])
            bridge.call.assert_not_called()

    async def test_invalid_directions_and_reverse_never_call_bridge(self):
        bridge = Mock()
        server = create_server(bridge)
        for direction in (-4, 1, 2, 3, 16, "4", True, False, 4.0, [], {}, float("nan")):
            with self.subTest(direction=direction):
                value = await self.invalid(
                    server, "rotate_entity", {"target_ref": "inserter", "direction": direction}
                )
                self.assertIn("direction", [e["field"] for e in value["detail"]["errors"]])
        for direction in (0, 4, 8, 12):
            await self.invalid(
                server,
                "rotate_entity",
                {"target_ref": "inserter", "direction": direction, "reverse": True},
            )
        value = await self.invalid(
            server, "rotate_entity", {"target_ref": "inserter", "direction": 2, "dir": 4}
        )
        self.assertEqual(
            {e["field"]: e["type"] for e in value["detail"]["errors"]},
            {"dir": "unexpected_argument", "direction": "literal_error"},
        )
        bridge.call.assert_not_called()

    async def test_rotation_arguments_and_measured_results_pass_through_in_one_request(self):
        bridge = Mock()
        server = create_server(bridge)
        for arguments, direction, rotations in (
            ({}, 4, 1),
            ({"reverse": True}, 12, 1),
            ({"direction": None}, 4, 1),
            *[({"direction": d}, d, d // 4) for d in (0, 4, 8, 12)],
        ):
            with self.subTest(arguments=arguments):
                result = {
                    "name": "inserter",
                    "direction": direction,
                    "position": {"surface": "nauvis", "x": 1.5, "y": 2.5},
                    "rotations": rotations,
                }
                bridge.call.reset_mock()
                bridge.call.return_value = {"tick": 42, "result": result}
                response = await server.call_tool("rotate_entity", {"target_ref": "i", **arguments})
                self.assertEqual(payload(response), {"ok": True, "tick": 42, **result})
                params = {"target_ref": "i", "reverse": arguments.get("reverse", False)}
                if arguments.get("direction") is not None:
                    params["direction"] = arguments["direction"]
                bridge.call.assert_called_once_with("rotate_entity", params)

    async def test_unreachable_detail_and_existing_errors_pass_through(self):
        bridge = Mock()
        server = create_server(bridge)
        details = [
            {"reason": "direction_unreachable", "direction": 0, "reachable_directions": [0, 4]},
            {"reason": "direction_unreachable", "direction": 0, "reachable_directions": [0]},
            {"entity": "wooden-chest", "reason": "not_rotatable"},
            {"entity": "inserter", "reason": "other_force"},
        ]
        for detail in details:
            with self.subTest(detail=detail):
                bridge.call.side_effect = BridgeError("invalid_target", "Cannot rotate.", detail)
                response = await server.call_tool("rotate_entity", {"target_ref": "target"})
                self.assertTrue(response.is_error)
                self.assertEqual(payload(response)["error"], "invalid_target")
                self.assertEqual(payload(response)["detail"], detail)

    async def test_schema_and_description(self):
        tools = {t.name: t for t in await create_server(None).list_tools()}
        tool = tools["rotate_entity"]
        self.assertEqual(
            set(tool.input_schema["properties"]), {"target_ref", "reverse", "direction"}
        )
        direction = tool.input_schema["properties"]["direction"]
        self.assertIsNone(direction["default"])
        self.assertEqual(direction["anyOf"][0]["enum"], [0, 4, 8, 12])
        for text in (
            "0/4/8/12 (north/east/south/west)",
            "up to 3 times",
            "reverse=true",
            "rotations: 1",
            "0 if already",
            "direction_unreachable",
            "restored original direction",
            "reachable_directions",
            INSERTER_DIRECTION_DESCRIPTION,
        ):
            self.assertIn(text, tool.description)
        self.assertIn("Unknown arguments are rejected", HINTS["invalid_input"])
