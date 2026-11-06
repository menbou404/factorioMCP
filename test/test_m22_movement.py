import copy
import json
import unittest
from unittest.mock import Mock

from factorio_mcp.server import HINTS, _normalize_error_detail, _normalize_result, create_server


class MovementTests(unittest.IsolatedAsyncioTestCase):
    async def test_description_and_hint_explain_adjusted_goals(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        description = tools["move_to"].description
        for text in (description, HINTS["path_not_found"]):
            for phrase in ("goal_adjusted", "original_goal", "within 10 tiles", "Without within"):
                with self.subTest(phrase=phrase, text=text):
                    self.assertIn(phrase, text)
        for phrase in (
            "With within set, the original goal is used unchanged",
            "Check a move's result before acting on targets near the destination",
            "out_of_range if the walk failed",
        ):
            self.assertIn(phrase, description)

    async def test_adjusted_positions_survive_start_poll_wait_and_cached_results(self):
        goals = {"goal_adjusted": {"x": 4.5, "y": 4}, "original_goal": {"x": 6, "y": 4}}
        start = {"action_id": "1", "status": "running", **goals}
        running = {**start, "type": "move", "detail": goals}
        final = {**running, "status": "succeeded", "position": {"x": 4.6, "y": 4}}
        for status in (running, final):
            for operation in ("get_action_status", "wait_for_action", "move_to"):
                with self.subTest(operation=operation, status=status["status"]):
                    raw = copy.deepcopy(status)
                    self.assertEqual(_normalize_result(operation, raw), status)
                    self.assertEqual(_normalize_error_detail(copy.deepcopy(goals)), goals)
        cached = {"original_response": {"result": copy.deepcopy(final)}}
        self.assertEqual(
            _normalize_result("get_request_result", cached)["original_response"]["result"], final
        )

        for operation, params, status in (
            ("move_to", {"surface": "nauvis", "x": 6, "y": 4}, start),
            ("get_action_status", {"action_id": "1"}, running),
            ("wait_for_action", {"action_id": "1", "timeout_seconds": 1}, final),
        ):
            with self.subTest(operation=operation):
                bridge = Mock()
                bridge.call.return_value = {"result": copy.deepcopy(status)}
                result = await create_server(bridge).call_tool(operation, params)
                self.assertFalse(result.is_error)
                value = json.loads(result.content[0].text)
                for field, position in goals.items():
                    self.assertEqual(value[field], position)
                    if operation != "move_to":
                        self.assertEqual(value["detail"][field], position)

        bridge = Mock()
        bridge.call.side_effect = [{"result": start}, {"result": final}]
        result = await create_server(bridge).call_tool(
            "move_to", {"surface": "nauvis", "x": 6, "y": 4, "wait_seconds": 1}
        )
        self.assertFalse(result.is_error)
        value = json.loads(result.content[0].text)
        for field, position in goals.items():
            self.assertEqual(value[field], position)
            self.assertEqual(value["detail"][field], position)

    async def test_absent_adjustment_stays_absent(self):
        for operation in ("move_to", "get_action_status", "wait_for_action"):
            result = _normalize_result(operation, {"status": "succeeded"})
            self.assertNotIn("goal_adjusted", result)
            self.assertNotIn("original_goal", result)
