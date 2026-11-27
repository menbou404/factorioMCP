import copy
import json
import re
import unittest
from pathlib import Path
from unittest.mock import Mock

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import create_server

ROOT = Path(__file__).resolve().parents[1]
CONTROL = (ROOT / "mod/factorio_ai_mcp_0.1.0/control.lua").read_text(encoding="utf-8")


def lua_function(name):
    match = re.search(rf"local function {name}\(.*?\nend", CONTROL, re.S)
    assert match is not None, name
    return match[0]


class ChunkGenerationTests(unittest.TestCase):
    def test_character_generation_is_chunk_and_surface_gated_without_forcing(self):
        helper = lua_function("generate_character_chunks")
        for text in (
            "if not valid_character(state) then return end",
            "math.floor(character.position.x / 32)",
            "math.floor(character.position.y / 32)",
            "state.last_generated_chunk",
            "last.x == cx and last.y == cy",
            "last.surface_index == character.surface.index",
            "character.surface.request_to_generate_chunks(character.position, 5)",
            "state.last_generated_chunk = {x = cx, y = cy, "
            "surface_index = character.surface.index}",
        ):
            self.assertIn(text, helper)
        self.assertLess(helper.index("return\n"), helper.index("request_to_generate_chunks"))
        self.assertNotIn("force_generate_chunk_requests", helper)
        spawn = lua_function("spawn_character")
        self.assertLess(
            spawn.index("state.character = character"),
            spawn.index("generate_character_chunks(state)"),
        )
        tick = CONTROL.split("script.on_event(defines.events.on_tick", 1)[1]
        self.assertIn("if game.tick % 30 == 0 then generate_character_chunks(state) end", tick)

    def test_goal_generation_precedes_every_path_request_and_move_terrain_queries(self):
        helper = lua_function("ensure_goal_chunk_generated")
        self.assertIn(
            "if not surface.is_chunk_generated("
            "{math.floor(goal.x / 32), math.floor(goal.y / 32)}) then",
            helper,
        )
        self.assertLess(
            helper.index("request_to_generate_chunks(goal, 1)"),
            helper.index("force_generate_chunk_requests()"),
        )
        request = lua_function("request_path")
        self.assertLess(
            request.index("goal = action.belt_step or goal"),
            request.index("ensure_goal_chunk_generated(character, goal)"),
        )
        self.assertLess(
            request.index("ensure_goal_chunk_generated(character, goal)"),
            request.index("character.surface.request_path{"),
        )
        move = lua_function("do_move")
        generated = move.index("ensure_goal_chunk_generated(character, goal)")
        self.assertLess(move.index("128 * 128"), generated)
        self.assertLess(generated, move.index("goal_obstacles(character, goal)"))
        self.assertLess(generated, move.index("find_non_colliding_position"))

    def test_path_diagnostic_reports_generation_without_overwriting_reasons(self):
        detail = lua_function("path_not_found_detail")
        for text in (
            "detail.goal_chunk_generated = character.surface.is_chunk_generated(",
            "{math.floor(goal.x / 32), math.floor(goal.y / 32)}",
            "if not detail.goal_chunk_generated and not detail.reason then",
            'detail.reason = "not_generated"',
            "The destination area is not generated yet; walk closer first "
            "(the world generates around the character).",
        ):
            self.assertIn(text, detail)
        self.assertNotIn("force_generate_chunk_requests", detail)

    def test_generation_helpers_do_not_observe_chart_or_modify_exploration(self):
        for name in ("generate_character_chunks", "ensure_goal_chunk_generated"):
            with self.subTest(name=name):
                helper = lua_function(name)
                for forbidden in (
                    "find_entities",
                    "find_tiles",
                    "record_explored_map",
                    "explored_map",
                    "register_target",
                    "observation",
                    ".chart",
                ):
                    self.assertNotIn(forbidden, helper)

    def test_lua_partial_lines_keep_status_reason_and_next_position(self):
        result = lua_function("line_action_result")
        for text in (
            'action.status == "succeeded" and action.line_result.complete == false',
            "result.partial = true",
            "result.stopped = action.line_result.stopped",
            "result.placed + result.already_present",
            "result.requested",
            "Continue from next_position.",
        ):
            self.assertIn(text, result)
        self.assertNotIn("action.status =", result.replace('action.status == "succeeded"', ""))
        self.assertIn("return line_action_result(action,", lua_function("do_place_line"))
        self.assertIn("line_action_result(action,", lua_function("do_action_status"))


class ChunkGenerationResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_line_fields_survive_start_poll_wait_and_cached_response(self):
        bridge = Mock()
        server = create_server(bridge)
        for stopped in ("missing_items", "missing_materials", "out_of_range", "blocked"):
            measured = {
                "action_id": "line-42",
                "type": "place_line",
                "status": "succeeded",
                "complete": False,
                "partial": True,
                "requested": 40,
                "placed": 20,
                "already_present": 2,
                "stopped": stopped,
                "next_position": {"x": 22.5, "y": 0.5},
                "last_position": {"x": 21.5, "y": 0.5},
                "first_position": {"x": 0.5, "y": 0.5},
                "direction": 4,
                "detail": {"reason": "fixture"},
                "hint": "The line stopped after 22 of 40 positions.",
            }
            for operation, args in (
                (
                    "place_line",
                    {
                        "item": "transport-belt",
                        "surface": "nauvis",
                        "from_x": 0.5,
                        "from_y": 0.5,
                        "to_x": 39.5,
                        "to_y": 0.5,
                        "walk": True,
                    },
                ),
                ("get_action_status", {"action_id": "line-42"}),
                ("wait_for_action", {"action_id": "line-42", "timeout_seconds": 1}),
                ("get_request_result", {"request_id": "request-42"}),
            ):
                with self.subTest(stopped=stopped, operation=operation):
                    value = copy.deepcopy(measured)
                    if operation == "get_request_result":
                        value = {"original_response": {"ok": True, "result": value}}
                    bridge.call.return_value = {"result": value}
                    response = await server.call_tool(operation, args)
                    result = json.loads(response.content[0].text)
                    self.assertFalse(response.is_error, result)
                    if operation == "get_request_result":
                        result = result["original_response"]["result"]
                    for field, expected in measured.items():
                        if field != "hint":
                            self.assertEqual(result[field], expected, field)
                    self.assertIn(f"22 of 40 positions ({stopped})", result["hint"])
                    self.assertIn("Continue from next_position.", result["hint"])

    async def test_full_running_failed_and_cancelled_lines_do_not_gain_partial(self):
        bridge = Mock()
        server = create_server(bridge)
        for status, complete in (
            ("succeeded", True),
            ("running", False),
            ("failed", False),
            ("cancelled", False),
        ):
            measured = {
                "type": "place_line",
                "status": status,
                "complete": complete,
                "requested": 40,
                "placed": 2,
                "already_present": 0,
            }
            if not complete:
                measured.update(stopped="out_of_range", next_position={"x": 2.5, "y": 0.5})
            bridge.call.return_value = {"result": measured}
            response = await server.call_tool("get_action_status", {"action_id": "line-42"})
            result = json.loads(response.content[0].text)
            self.assertEqual(result["status"], status)
            self.assertNotIn("partial", result)

    async def test_path_errors_preserve_both_generation_values_and_specific_hint(self):
        bridge = Mock()
        server = create_server(bridge)
        for generated in (True, False):
            detail = {"goal_chunk_generated": generated}
            if not generated:
                detail.update(
                    reason="not_generated",
                    hint=(
                        "The destination area is not generated yet; walk closer first "
                        "(the world generates around the character)."
                    ),
                )
            bridge.call.side_effect = BridgeError("path_not_found", detail=detail)
            response = await server.call_tool("move_to", {"surface": "nauvis", "x": 100, "y": 0})
            result = json.loads(response.content[0].text)
            self.assertTrue(response.is_error)
            self.assertEqual(result["detail"], detail)
            if not generated:
                self.assertEqual(result["hint"], detail["hint"])


if __name__ == "__main__":
    unittest.main()
