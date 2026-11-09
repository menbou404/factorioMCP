import copy
import json
import unittest
from unittest.mock import Mock

from mcp.server.mcpserver.exceptions import ToolError

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import _normalize_result, create_server

LINE = {
    "item": "transport-belt",
    "surface": "nauvis",
    "from_x": 0.5,
    "from_y": 2.5,
    "to_x": 4.5,
    "to_y": 2.5,
}


class PlaceLineTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_schema_and_description(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        tool = tools["place_line"]
        self.assertEqual(set(tool.input_schema["required"]), set(LINE))
        for phrase in (
            "2-64 tiles",
            "tile centres",
            "transport-belt or pipe",
            "does not walk",
            "same reach, observation and inventory checks",
            "Build reach is 10 tiles",
            "near the middle",
            "about 19 tiles per call",
            "stops at the first tile",
            "keeps earlier tiles",
            "walk near next_position",
            "call again from it",
            "already placed tiles are skipped",
            "previous line's last belt points at",
            "place_entity for single tiles and undergrounds",
            "crafting is allowed",
            "ok: true even if placed=0",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, tool.description)

    async def test_strict_argument_validation_never_calls_bridge(self):
        bridge = Mock()
        server = create_server(bridge)
        invalid = []
        for key in ("item", "surface"):
            invalid.extend({**LINE, key: value} for value in (None, "", 3, True, []))
        for key in ("from_x", "from_y", "to_x", "to_y"):
            invalid.extend(
                {**LINE, key: value}
                for value in (None, "1", True, [], float("nan"), float("inf"), -1000001, 1000001)
            )
        invalid.extend(
            [
                {**LINE, "to_y": 3.5},  # diagonal
                {**LINE, "to_x": 0.9},  # one snapped tile
                {**LINE, "to_x": 64.5},  # 65 inclusive
            ]
        )
        for key in LINE:
            invalid.append({name: value for name, value in LINE.items() if name != key})
        for params in invalid:
            with self.subTest(params=params):
                try:
                    response = await server.call_tool("place_line", params)
                except ToolError as error:
                    value = json.loads(str(error))
                else:
                    value = json.loads(response.content[0].text)
                    self.assertTrue(response.is_error)
                self.assertEqual(value["error"], "invalid_input")
        bridge.call.assert_not_called()

    async def test_valid_endpoints_are_forwarded_without_snapping_or_surface_guessing(self):
        bridge = Mock()
        bridge.call.return_value = {"result": {"complete": True}}
        server = create_server(bridge)
        for start, end in (
            ((0, 2), (1, 2)),
            ((0.1, 2.1), (63.9, 2.9)),
            ((-0.1, -0.1), (-63.1, -0.9)),
            ((0, 2), (0, -61)),
            ((0, 2), (0, 65)),
        ):
            params = {
                **LINE,
                "surface": "custom",
                "from_x": start[0],
                "from_y": start[1],
                "to_x": end[0],
                "to_y": end[1],
            }
            with self.subTest(params=params):
                response = await server.call_tool("place_line", params)
                self.assertFalse(response.is_error)
                bridge.call.assert_called_with("place_line", params)

    async def test_stopped_result_preserves_measured_progress_and_adds_hint(self):
        bridge = Mock()
        server = create_server(bridge)
        for stopped, detail in (
            ("out_of_range", {"reach": 10}),
            ("blocked", {"reason": "different_direction", "existing_direction": 0}),
            ("missing_materials", {"item": "transport-belt"}),
            ("internal_error", None),
        ):
            for placed in (0, 2):
                measured = {
                    "item": "transport-belt",
                    "name": "transport-belt",
                    "direction": 4,
                    "requested": 5,
                    "placed": placed,
                    "already_present": 0,
                    "complete": False,
                    "first_position": {"x": 0.5, "y": 2.5},
                    "next_position": {"x": placed + 0.5, "y": 2.5},
                    "stopped": stopped,
                    "collected_items": [{"name": "wood", "quality": "normal", "count": 2}],
                }
                if placed:
                    measured["last_position"] = {"x": placed - 0.5, "y": 2.5}
                if detail:
                    measured["detail"] = detail
                with self.subTest(stopped=stopped, placed=placed):
                    bridge.call.return_value = {"tick": 42, "result": copy.deepcopy(measured)}
                    response = await server.call_tool("place_line", LINE)
                    value = json.loads(response.content[0].text)
                    self.assertFalse(response.is_error)
                    self.assertTrue(value.pop("ok"))
                    self.assertEqual(value.pop("tick"), 42)
                    hint = value.pop("hint")
                    self.assertIn("next_position", hint)
                    self.assertIn("place_entity if only one tile remains", hint)
                    self.assertEqual(value, measured)
                    cached = {"original_response": {"result": copy.deepcopy(measured)}}
                    recovered = _normalize_result("get_request_result", cached)
                    self.assertEqual(recovered["original_response"]["result"]["hint"], hint)

    async def test_complete_repeat_and_mod_errors_are_not_reinterpreted(self):
        bridge = Mock()
        server = create_server(bridge)
        measured = {"requested": 5, "placed": 0, "already_present": 5, "complete": True}
        bridge.call.return_value = {"result": measured}
        response = await server.call_tool("place_line", LINE)
        value = json.loads(response.content[0].text)
        self.assertFalse(response.is_error)
        self.assertEqual(value, {"ok": True, "tick": None, **measured})
        for code, detail in (
            ("invalid_target", {"reason": "not_line_placeable"}),
            ("invalid_input", None),
            ("action_conflict", {"running_action": "move-1"}),
        ):
            bridge.call.side_effect = BridgeError(code, code, detail)
            response = await server.call_tool("place_line", LINE)
            value = json.loads(response.content[0].text)
            self.assertTrue(response.is_error)
            self.assertFalse(value["ok"])
            self.assertEqual(value["error"], code)
            if detail:
                self.assertEqual(value["detail"], detail)
