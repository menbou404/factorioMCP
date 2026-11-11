import copy
import json
import unittest
from unittest.mock import Mock

from mcp.server.mcpserver.exceptions import ToolError

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import create_server

LINE = {
    "item": "transport-belt",
    "surface": "nauvis",
    "from_x": 2.5,
    "from_y": 2.5,
    "to_x": 0.5,
    "to_y": 2.5,
}
TOOLS = ("place_line", "check_line")


def payload(response):
    return json.loads(response.content[0].text)


class PathCornerTests(unittest.IsolatedAsyncioTestCase):
    async def invalid(self, server, tool, args, field=None):
        try:
            response = await server.call_tool(tool, args)
        except ToolError as exc:
            value = json.loads(str(exc))
        else:
            self.assertTrue(response.is_error)
            value = payload(response)
        self.assertEqual(value["error"], "invalid_input")
        if field:
            errors = value["detail"]["errors"]
            self.assertIn(field, [e["field"] for e in errors])
            for error in errors:
                self.assertEqual(set(error), {"field", "type", "message"})
                self.assertTrue(error["message"])
        return value

    async def test_strict_end_direction_validation(self):
        bridge = Mock()
        server = create_server(bridge)
        for tool in TOOLS:
            for direction in (-4, 1, 2, 3, 16, "0", True, False, 0.0, 4.0, [], {}, float("nan")):
                with self.subTest(tool=tool, direction=direction):
                    await self.invalid(
                        server, tool, {**LINE, "end_direction": direction}, "end_direction"
                    )
        bridge.call.assert_not_called()

    async def test_all_axes_reject_only_opposite_and_forward_valid_corners(self):
        bridge = Mock()
        bridge.call.return_value = {"result": {"complete": True}}
        server = create_server(bridge)
        # Include negative/fractional coordinates: validation follows snapped tiles.
        for dx, dy, direction in ((2, 0, 4), (-2, 0, 12), (0, 2, 8), (0, -2, 0)):
            line = {**LINE, "from_x": -0.1, "from_y": -0.1, "to_x": dx - 0.9, "to_y": dy - 0.9}
            for tool in TOOLS:
                for end in (0, 4, 8, 12):
                    bridge.call.reset_mock()
                    args = {**line, "end_direction": end}
                    with self.subTest(tool=tool, direction=direction, end=end):
                        if end == (direction + 8) % 16:
                            await self.invalid(server, tool, args, "end_direction")
                            bridge.call.assert_not_called()
                        else:
                            self.assertFalse((await server.call_tool(tool, args)).is_error)
                            bridge.call.assert_called_once_with(tool, args)

    async def test_endpoint_checks_still_apply_with_end_direction(self):
        bridge = Mock()
        server = create_server(bridge)
        for tool in TOOLS:
            for overrides in (
                {"to_y": 1.5},  # diagonal
                {"to_x": 2.9},  # same snapped tile
                {"to_x": 66.5},  # 65 inclusive
            ):
                with self.subTest(tool=tool, overrides=overrides):
                    await self.invalid(server, tool, {**LINE, **overrides, "end_direction": 0})
        bridge.call.assert_not_called()

    async def test_omitted_and_null_follow_m33_optional_direction_convention(self):
        bridge = Mock()
        bridge.call.return_value = {"result": {"complete": True}}
        server = create_server(bridge)
        for tool in TOOLS:
            for extra in ({}, {"end_direction": None}):
                self.assertFalse((await server.call_tool(tool, {**LINE, **extra})).is_error)
                bridge.call.assert_called_with(tool, LINE)

    async def test_measured_results_and_mod_errors_pass_through(self):
        bridge = Mock()
        server = create_server(bridge)
        for tool in TOOLS:
            for complete in (True, False):
                measured = {
                    "direction": 12,
                    "end_direction": 0,
                    "requested": 3,
                    "complete": complete,
                }
                if tool == "place_line":
                    measured.update(placed=0, already_present=3 if complete else 2)
                    if not complete:
                        measured.update(
                            stopped="blocked",
                            detail={"reason": "different_direction", "existing_direction": 8},
                            next_position={"x": 0.5, "y": 2.5},
                        )
                else:
                    measured.update(
                        clear=0,
                        already_present=3 if complete else 2,
                        problem_total=0 if complete else 1,
                        problems=[]
                        if complete
                        else [
                            {
                                "x": 0.5,
                                "y": 2.5,
                                "reason": "different_direction",
                                "existing_direction": 8,
                            }
                        ],
                    )
                bridge.call.return_value = {"tick": 42, "result": copy.deepcopy(measured)}
                value = payload(await server.call_tool(tool, {**LINE, "end_direction": 0}))
                value.pop("hint", None)
                self.assertEqual(value, {"ok": True, "tick": 42, **measured})
            detail = {"reason": "end_direction_requires_transport_belt"}
            bridge.call.side_effect = BridgeError("invalid_input", "Invalid line.", detail)
            response = await server.call_tool(tool, {**LINE, "item": "pipe", "end_direction": 0})
            self.assertTrue(response.is_error)
            self.assertEqual(payload(response)["error"], "invalid_input")
            self.assertEqual(payload(response)["detail"], detail)
            bridge.call.side_effect = None

    async def test_schemas_and_descriptions(self):
        tools = {t.name: t for t in await create_server(None).list_tools()}
        for name in TOOLS:
            tool = tools[name]
            self.assertEqual(set(tool.input_schema["properties"]), {*LINE, "end_direction"})
            self.assertEqual(set(tool.input_schema["required"]), set(LINE))
            schema = tool.input_schema["properties"]["end_direction"]
            self.assertIsNone(schema["default"])
            self.assertEqual(schema["anyOf"][0]["enum"], [0, 4, 8, 12])
            for phrase in (
                "only the last belt",
                "all other tiles keep the line direction",
                "westward line",
                "end_direction 0",
                "face north",
                "next northward line",
                "without rotate_entity",
                "exact opposite",
                "non-belt",
                "pipe",
                "invalid_input",
                "Results include end_direction when given",
                "direction stays the line direction",
            ):
                with self.subTest(tool=name, phrase=phrase):
                    self.assertIn(phrase, tool.description)
