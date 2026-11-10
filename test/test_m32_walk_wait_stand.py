import copy
import json
import unittest
from unittest.mock import Mock, patch

from factorio_mcp.bridge import BridgeError, FactorioBridge
from factorio_mcp.server import RUNNING_WALK_HINT, _normalize_result, create_server

PLACE = {"item": "stone-furnace", "surface": "nauvis", "x": 2, "y": 2}
TRANSFER = {
    "target_ref": "chest",
    "direction": "to_entity",
    "inventory_role": "main",
    "item": "coal",
    "count": 1,
}


def conflict(kind="move", action_id="walk"):
    return {
        "ok": False,
        "error": "action_conflict",
        "detail": {"running_action": {"id": action_id, "type": kind, "remaining_distance": 30}},
    }


def error(kind="move"):
    entry = conflict(kind)
    return BridgeError(entry["error"], "busy", entry["detail"])


def status(state):
    return {
        "result": {
            "action_id": "walk",
            "type": "move",
            "status": state,
            "remaining_distance": 12.34,
        }
    }


def payload(response):
    return json.loads(response.content[0].text)


class WalkWaitStandTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_write_tools_wait_and_retry_once(self):
        calls = (
            ("place_entity", PLACE),
            (
                "place_line",
                {
                    "item": "pipe",
                    "surface": "nauvis",
                    "from_x": 0,
                    "from_y": 0,
                    "to_x": 2,
                    "to_y": 0,
                },
            ),
            ("transfer_item", TRANSFER),
            ("mine_entity", {"target_ref": "ore"}),
            ("move_to", {"surface": "nauvis", "x": 10, "y": 0}),
        )
        for operation, params in calls:
            with self.subTest(operation=operation):
                bridge = Mock()
                bridge.call.side_effect = [
                    error(),
                    status("succeeded"),
                    {"result": {"status": "succeeded", "retried": True}},
                ]
                result = payload(await create_server(bridge).call_tool(operation, params))
                self.assertTrue(result["retried"])
                self.assertEqual(result["waited_for_walk"]["action_id"], "walk")
                self.assertEqual(result["waited_for_walk"]["status"], "succeeded")
                seconds = result["waited_for_walk"]["seconds"]
                self.assertEqual(seconds, round(seconds, 2))
                self.assertEqual(
                    [c.args[0] for c in bridge.call.call_args_list],
                    [operation, "get_action_status", operation],
                )
                self.assertEqual(bridge.call.call_args_list[0], bridge.call.call_args_list[2])

    async def test_mine_and_craft_conflicts_never_wait(self):
        for kind in ("mine", "craft"):
            bridge = Mock()
            bridge.call.side_effect = error(kind)
            result = payload(await create_server(bridge).call_tool("place_entity", PLACE))
            self.assertEqual(result["error"], "action_conflict")
            self.assertNotIn("waited_for_walk", result)
            self.assertEqual(bridge.call.call_count, 1)

    async def test_timeout_returns_original_conflict_and_current_distance(self):
        bridge = Mock()
        bridge.call.side_effect = [error(), status("running")]
        with patch("factorio_mcp.server.WALK_WAIT_SECONDS", 0):
            result = payload(await create_server(bridge).call_tool("place_entity", PLACE))
        self.assertEqual(result["error"], "action_conflict")
        self.assertEqual(result["waited_for_walk"]["status"], "running")
        self.assertEqual(result["detail"]["running_action"]["remaining_distance"], 12.34)
        self.assertIn("wait_for_action", result["hint"])
        self.assertEqual(bridge.call.call_count, 2)

    async def test_failed_cancelled_walk_still_retries_and_preserves_retry_error(self):
        for state in ("failed", "cancelled"):
            bridge = Mock()
            bridge.call.side_effect = [
                error(),
                status(state),
                BridgeError("out_of_range", "too far", {"reach": 10}),
            ]
            response = await create_server(bridge).call_tool("place_entity", PLACE)
            result = payload(response)
            self.assertTrue(response.is_error)
            self.assertEqual(result["error"], "out_of_range")
            self.assertEqual(result["waited_for_walk"]["status"], state)
            self.assertEqual(bridge.call.call_count, 3)

    async def test_retry_conflict_does_not_wait_again(self):
        bridge = Mock()
        bridge.call.side_effect = [error(), status("succeeded"), error()]
        result = payload(await create_server(bridge).call_tool("place_entity", PLACE))
        self.assertEqual(result["error"], "action_conflict")
        self.assertEqual(bridge.call.call_count, 3)
        self.assertIn("waited_for_walk", result)

    async def test_batches_only_retry_when_every_item_conflicts_from_same_walk(self):
        for operation, params in (("place_entities", PLACE), ("transfer_items", TRANSFER)):
            cases = (
                ([conflict(), conflict()], True),
                ([conflict(), {"ok": True, "result": {"placed": True}}], False),
                ([conflict(), {"ok": False, "error": "out_of_range"}], False),
                ([conflict(), conflict("mine")], False),
                ([conflict(), conflict(action_id="another")], False),
                ([], False),
            )
            for entries, retry in cases:
                with self.subTest(operation=operation, entries=entries):
                    bridge = Mock()
                    original = {"result": {"results": copy.deepcopy(entries)}}
                    bridge.call.side_effect = [
                        original,
                        status("succeeded"),
                        {"result": {"results": [{"ok": True, "result": {}}]}},
                    ]
                    result = payload(
                        await create_server(bridge).call_tool(
                            operation, {"items": [params, params]}
                        )
                    )
                    self.assertEqual("waited_for_walk" in result, retry)
                    self.assertEqual(bridge.call.call_count, 3 if retry else 1)
                    if not retry:
                        self.assertEqual(
                            [e["ok"] for e in result["results"]], [e["ok"] for e in entries]
                        )

    async def test_batch_timeout_keeps_all_conflicts_and_top_level_metadata(self):
        bridge = Mock()
        bridge.call.side_effect = [
            {"result": {"results": [conflict(), conflict()]}},
            status("running"),
        ]
        with patch("factorio_mcp.server.WALK_WAIT_SECONDS", 0):
            result = payload(
                await create_server(bridge).call_tool("place_entities", {"items": [PLACE, PLACE]})
            )
        self.assertEqual(result["waited_for_walk"]["status"], "running")
        for entry in result["results"]:
            self.assertEqual(entry["detail"]["running_action"]["remaining_distance"], 12.34)
            self.assertNotIn("waited_for_walk", entry)

    async def test_action_wait_budget_uses_unrounded_walk_wait(self):
        for operation, params, kind in (
            ("move_to", {"surface": "nauvis", "x": 10, "y": 0}, "move"),
            ("mine_entity", {"target_ref": "ore", "count": 1}, "mine"),
        ):
            for wait_seconds in (1, 50):
                with self.subTest(operation=operation, wait_seconds=wait_seconds):
                    clock = Mock()
                    clock.monotonic.return_value = 0.0

                    async def advance(seconds, clock=clock):
                        clock.monotonic.return_value += seconds

                    def respond(
                        name, arguments, operation=operation, params=params, kind=kind, clock=clock
                    ):
                        if name == operation:
                            self.assertEqual(arguments, params)
                            if clock.monotonic.return_value == 0:
                                raise error()
                        else:
                            self.assertEqual(name, "get_action_status")
                            if arguments == {"action_id": "walk"}:
                                clock.monotonic.return_value = 14.123
                                return status("succeeded")
                            self.assertEqual(arguments, {"action_id": "next"})
                        return {"result": {"action_id": "next", "type": kind, "status": "running"}}

                    bridge = Mock()
                    bridge.call.side_effect = respond

                    with (
                        patch("factorio_mcp.server.time", clock),
                        patch("factorio_mcp.server.asyncio.sleep", side_effect=advance),
                    ):
                        result = payload(
                            await create_server(bridge).call_tool(
                                operation, {**params, "wait_seconds": wait_seconds}
                            )
                        )
                    self.assertTrue(result["timed_out"])
                    self.assertAlmostEqual(result["waited_seconds"], min(wait_seconds, 50 - 14.123))
                    self.assertLessEqual(clock.monotonic.return_value, 50)
                    self.assertEqual(result["waited_for_walk"]["seconds"], 14.12)

    async def test_exhausted_walk_budget_returns_start_without_polling(self):
        for operation, params, kind in (
            ("move_to", {"surface": "nauvis", "x": 10, "y": 0}, "move"),
            ("mine_entity", {"target_ref": "ore", "count": 1}, "mine"),
        ):
            for seconds in (50, 50.123):
                with self.subTest(operation=operation, seconds=seconds):
                    start = {"action_id": "next", "type": kind, "status": "running"}
                    bridge = Mock()
                    bridge.call.side_effect = [
                        error(),
                        status("succeeded"),
                        {"tick": 100, "result": start},
                    ]
                    with (
                        patch("factorio_mcp.server.time") as clock,
                        patch("factorio_mcp.server.asyncio.sleep") as sleep,
                    ):
                        clock.monotonic.side_effect = [0, 0, seconds]
                        result = payload(
                            await create_server(bridge).call_tool(
                                operation, {**params, "wait_seconds": 50}
                            )
                        )
                    expected = {
                        "ok": True,
                        "tick": 100,
                        **start,
                        "waited_for_walk": {
                            "action_id": "walk",
                            "status": "succeeded",
                            "seconds": round(seconds, 2),
                        },
                    }
                    if kind == "move":
                        expected["hint"] = RUNNING_WALK_HINT.format(action_id="next")
                    self.assertEqual(result, expected)
                    self.assertEqual(
                        [entry.args[0] for entry in bridge.call.call_args_list],
                        [operation, "get_action_status", operation],
                    )
                    sleep.assert_not_awaited()

    async def test_stand_positions_passthrough_and_empty_normalization(self):
        for positions in ({}, [], [{"x": 1.25, "y": 3}]):
            expected = positions or []
            bridge = Mock()
            bridge.call.side_effect = BridgeError(
                "blocked",
                "blocked",
                {"reason": "character_in_the_way", "stand_positions": copy.deepcopy(positions)},
            )
            result = payload(await create_server(bridge).call_tool("place_entity", PLACE))
            self.assertEqual(result["detail"]["stand_positions"], expected)
            for operation, measured in (
                (
                    "check_placement",
                    {
                        "blocked_reason": "character_in_the_way",
                        "stand_positions": copy.deepcopy(positions),
                    },
                ),
                (
                    "place_entities",
                    {
                        "results": [
                            {
                                "ok": False,
                                "error": "blocked",
                                "detail": {"stand_positions": copy.deepcopy(positions)},
                            }
                        ]
                    },
                ),
                (
                    "get_request_result",
                    {
                        "original_response": {
                            "result": {"stand_positions": copy.deepcopy(positions)}
                        }
                    },
                ),
            ):
                normalized = _normalize_result(operation, measured)
                if operation == "place_entities":
                    normalized = normalized["results"][0]["detail"]
                elif operation == "get_request_result":
                    normalized = normalized["original_response"]["result"]
                self.assertEqual(normalized["stand_positions"], expected)

    async def test_descriptions_and_hints(self):
        tools = {t.name: t for t in await create_server(None).list_tools()}
        for name in (
            "place_entity",
            "place_entities",
            "place_line",
            "transfer_item",
            "transfer_items",
            "mine_entity",
            "move_to",
            "wait_for_action",
        ):
            self.assertIn("15 seconds", tools[name].description)
            self.assertIn("waited_for_walk", tools[name].description)
        for name in ("place_entity", "place_entities", "check_placement"):
            for phrase in ("stand_positions", "move_to", "automatically"):
                self.assertIn(phrase, tools[name].description)
        for name in ("move_to", "mine_entity"):
            self.assertIn(
                "Automatic walk wait plus wait_seconds is capped at 50 seconds",
                tools[name].description,
            )
        self.assertIn("15 seconds", RUNNING_WALK_HINT)
        self.assertIn("For longer walks", RUNNING_WALK_HINT)

    async def test_retry_uses_fresh_bridge_request_id(self):
        requests = []

        def send(_host, _port, _password, command):
            request = json.loads(command.removeprefix("/ai_mcp "))
            requests.append(request)
            response = {
                "protocol_version": 1,
                "request_id": request["request_id"],
                "ok": True,
                "result": {},
            }
            if len(requests) == 1:
                response.update(conflict())
            elif request["operation"] == "get_action_status":
                response.update(status("succeeded"))
            else:
                response["result"] = {"placed": True}
            return json.dumps(response)

        result = payload(
            await create_server(FactorioBridge(password="test", send=send)).call_tool(
                "place_entity", PLACE
            )
        )
        self.assertTrue(result["placed"])
        self.assertEqual(len({r["request_id"] for r in requests}), 3)
        self.assertEqual(requests[0]["params"], requests[-1]["params"])


if __name__ == "__main__":
    unittest.main()
