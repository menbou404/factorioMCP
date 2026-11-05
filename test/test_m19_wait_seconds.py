import json
import unittest
from unittest.mock import Mock, call, patch

from mcp.server.mcpserver.exceptions import ToolError

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import create_server


def payload(result):
    return json.loads(result.content[0].text)


CASES = (
    ("move_to", {"surface": "nauvis", "x": 2, "y": 3, "within": 1}, {"within": 1}),
    ("craft", {"recipe": "iron-gear-wheel", "count": 1}, {"started": 1}),
)


class WaitSecondsTests(unittest.IsolatedAsyncioTestCase):
    async def run_with_clock(self, operation, arguments, status_at, start=None):
        elapsed = 0.0

        async def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        def respond(name, params):
            if name == operation and operation != "wait_for_action":
                return {"tick": 10, "result": start}
            self.assertEqual(name, "get_action_status")
            self.assertEqual(params, {"action_id": "action-1"})
            state = status_at(elapsed)
            if isinstance(state, Exception):
                raise state
            return {"tick": 20, "result": state}

        bridge = Mock()
        bridge.call.side_effect = respond
        server = create_server(bridge)
        with (
            patch("factorio_mcp.server.time") as clock,
            patch("factorio_mcp.server.asyncio.sleep", side_effect=advance) as sleep,
        ):
            clock.monotonic.side_effect = lambda: elapsed
            result = await server.call_tool(operation, arguments)
        return result, bridge, sleep, elapsed

    async def test_omitted_or_null_wait_returns_start_unchanged(self):
        for operation, arguments, extra in CASES:
            for optional in ({}, {"wait_seconds": None}):
                with self.subTest(operation=operation, optional=optional):
                    start = {"action_id": "action-1", "status": "running", **extra}
                    bridge = Mock()
                    bridge.call.return_value = {"tick": 10, "result": start}
                    with patch("factorio_mcp.server.asyncio.sleep") as sleep:
                        result = await create_server(bridge).call_tool(
                            operation, {**arguments, **optional}
                        )
                    self.assertFalse(result.is_error)
                    self.assertEqual(payload(result), {"ok": True, "tick": 10, **start})
                    bridge.call.assert_called_once_with(operation, arguments)
                    sleep.assert_not_awaited()

    async def test_wait_succeeds_and_preserves_start_fields_with_final_fields_winning(self):
        for operation, arguments, extra in CASES:
            with self.subTest(operation=operation):
                start = {
                    "action_id": "action-1",
                    "status": "running",
                    "detail": {"old": True},
                    "estimated_seconds": 0.5,
                    **extra,
                }
                final = {
                    "action_id": "action-1",
                    "type": "move" if operation == "move_to" else "craft",
                    "status": "succeeded",
                    "detail": {"finished": True},
                    "position": {"x": 2, "y": 3},
                }
                result, bridge, sleep, elapsed = await self.run_with_clock(
                    operation,
                    {**arguments, "wait_seconds": 1},
                    lambda t, final=final: final if t >= 0.5 else {**final, "status": "running"},
                    start,
                )
                self.assertFalse(result.is_error)
                self.assertEqual(
                    payload(result),
                    {
                        "ok": True,
                        "tick": 20,
                        **start,
                        **final,
                        "timed_out": False,
                        "waited_seconds": 0.5,
                    },
                )
                self.assertEqual(elapsed, 0.5)
                self.assertEqual(sleep.await_count, 2)
                sleep.assert_awaited_with(0.25)
                self.assertEqual(
                    bridge.call.call_args_list,
                    [call(operation, arguments)]
                    + [call("get_action_status", {"action_id": "action-1"})] * 3,
                )

    async def test_timeout_keeps_action_id_and_does_not_cancel_and_can_resume(self):
        for operation, arguments, extra in CASES:
            with self.subTest(operation=operation):
                start = {"action_id": "action-1", "status": "running", **extra}
                running = {"action_id": "action-1", "type": operation, "status": "running"}
                result, bridge, sleep, elapsed = await self.run_with_clock(
                    operation,
                    {**arguments, "wait_seconds": 1.1},
                    lambda _, running=running: running,
                    start,
                )
                self.assertFalse(result.is_error)
                value = payload(result)
                self.assertEqual(
                    value,
                    {
                        "ok": True,
                        "tick": 20,
                        **start,
                        **running,
                        "timed_out": True,
                        "waited_seconds": 1.1,
                    },
                )
                self.assertEqual(elapsed, 1.1)
                self.assertAlmostEqual(sleep.await_args.args[0], 0.1)
                self.assertEqual(bridge.call.call_count, 7)
                self.assertTrue(
                    all(
                        entry.args[0] == "get_action_status"
                        for entry in bridge.call.call_args_list[1:]
                    )
                )
                bridge.call.side_effect = None
                bridge.call.return_value = {"result": {**running, "status": "succeeded"}}
                resumed = await create_server(bridge).call_tool(
                    "wait_for_action", {"action_id": value["action_id"]}
                )
                self.assertEqual(payload(resumed)["status"], "succeeded")
                self.assertFalse(payload(resumed)["timed_out"])

    async def test_start_error_is_identical_with_and_without_wait(self):
        for operation, arguments, _ in CASES:
            for code in ("out_of_range", "missing_materials", "action_conflict"):
                with self.subTest(operation=operation, code=code):
                    bridge = Mock()
                    bridge.call.side_effect = BridgeError(
                        code, "Start failed", {"request_id": "req", "reason": "test"}
                    )
                    server = create_server(bridge)
                    expected = await server.call_tool(operation, arguments)
                    bridge.call.reset_mock()
                    result = await server.call_tool(operation, {**arguments, "wait_seconds": 1})
                    self.assertTrue(result.is_error)
                    self.assertEqual(payload(result), payload(expected))
                    bridge.call.assert_called_once_with(operation, arguments)

    async def test_terminal_start_is_returned_without_polling(self):
        for operation, arguments, extra in CASES:
            for status in ("succeeded", "failed", "cancelled"):
                with self.subTest(operation=operation, status=status):
                    start = {"action_id": "action-1", "status": status, **extra}
                    if operation == "move_to":
                        start["distance"] = 0.25
                    bridge = Mock()
                    bridge.call.return_value = {"tick": 10, "result": start}
                    with patch("factorio_mcp.server.asyncio.sleep") as sleep:
                        result = await create_server(bridge).call_tool(
                            operation, {**arguments, "wait_seconds": 1}
                        )
                    self.assertEqual(payload(result), {"ok": True, "tick": 10, **start})
                    bridge.call.assert_called_once_with(operation, arguments)
                    sleep.assert_not_awaited()

    async def test_poll_error_matches_wait_for_action(self):
        error = BridgeError("action_not_found", "Lost action", {"request_id": "poll"})
        expected, _, _, _ = await self.run_with_clock(
            "wait_for_action", {"action_id": "action-1"}, lambda _: error
        )
        for operation, arguments, extra in CASES:
            with self.subTest(operation=operation):
                result, bridge, _, _ = await self.run_with_clock(
                    operation,
                    {**arguments, "wait_seconds": 1},
                    lambda t: error if t else {"status": "running"},
                    {"action_id": "action-1", "status": "running", **extra},
                )
                self.assertTrue(result.is_error)
                self.assertEqual(payload(result), payload(expected))
                self.assertEqual(bridge.call.call_count, 3)

    async def test_wait_for_action_output_and_polling_unchanged(self):
        for status in ("succeeded", "failed", "cancelled", "queued", "running"):
            with self.subTest(status=status):
                state = {
                    "action_id": "action-1",
                    "type": "craft",
                    "status": status,
                    "detail": {"count": 1, "remaining": 0},
                }
                result, bridge, sleep, elapsed = await self.run_with_clock(
                    "wait_for_action",
                    {"action_id": "action-1", "timeout_seconds": 1},
                    lambda t, state=state: state if t >= 0.5 else {**state, "status": "running"},
                )
                waited = 1 if status == "running" else 0.5
                self.assertEqual(
                    payload(result),
                    {
                        "ok": True,
                        "tick": 20,
                        **state,
                        "timed_out": status == "running",
                        "waited_seconds": waited,
                    },
                )
                self.assertFalse(result.is_error)
                self.assertEqual(elapsed, waited)
                self.assertEqual(sleep.await_count, waited * 4)
                self.assertEqual(bridge.call.call_count, waited * 4 + 1)

    async def test_schema_bounds_and_descriptions(self):
        bridge = Mock()
        bridge.call.return_value = {"result": {"action_id": "action-1", "status": "succeeded"}}
        server = create_server(bridge)
        tools = {tool.name: tool for tool in await server.list_tools()}
        for operation, arguments, _ in CASES:
            schema = tools[operation].input_schema
            self.assertNotIn("wait_seconds", schema["required"])
            wait = schema["properties"]["wait_seconds"]
            self.assertIsNone(wait["default"])
            numeric = next(part for part in wait["anyOf"] if part["type"] == "number")
            self.assertEqual(numeric["minimum"], 1)
            self.assertEqual(numeric["maximum"], 50)
            for seconds in (1, 50):
                result = await server.call_tool(operation, {**arguments, "wait_seconds": seconds})
                self.assertEqual(payload(result)["status"], "succeeded")
            bridge.call.reset_mock()
            for seconds in (0, 51, float("inf"), float("-inf"), float("nan")):
                with self.subTest(operation=operation, seconds=seconds):
                    with self.assertRaises(ToolError):
                        await server.call_tool(operation, {**arguments, "wait_seconds": seconds})
            bridge.call.assert_not_called()
        self.assertNotIn("wait_seconds", tools["mine_entity"].input_schema["properties"])
        for operation in ("move_to", "craft", "wait_for_action"):
            for text in (
                "wait_seconds",
                "Prefer" if operation != "wait_for_action" else "prefer",
                "60-second",
                "timeout",
            ):
                self.assertIn(text, tools[operation].description)


if __name__ == "__main__":
    unittest.main()
