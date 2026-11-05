import copy
import json
import unittest
from unittest.mock import Mock, patch

from mcp.server.mcpserver.exceptions import ToolError

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import HINTS, OPERATION_HINTS, create_server


def payload(result):
    return json.loads(result.content[0].text)


class DrillResourceDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_placement_normalizes_empty_resources_and_keeps_reason(self):
        bridge = Mock()
        bridge.call.return_value = {
            "result": {
                "placeable": False,
                "reason": "blocked",
                "blocked_reason": "placement_rules",
                "reason_detail": "no_resources_in_mining_area",
                "mining_area_resources": {},
            }
        }
        arguments = {"item": "burner-mining-drill", "surface": "nauvis", "x": 2, "y": 3}
        result = await create_server(bridge).call_tool("check_placement", arguments)
        self.assertFalse(result.is_error)
        value = payload(result)
        self.assertEqual(value["mining_area_resources"], [])
        self.assertEqual(value["reason_detail"], "no_resources_in_mining_area")
        self.assertEqual(value["blocked_reason"], "placement_rules")
        bridge.call.assert_called_once_with("check_placement", {**arguments, "direction": 0})

    async def test_resources_survive_checks_errors_and_batch_errors(self):
        arguments = {"item": "burner-mining-drill", "surface": "nauvis", "x": 2, "y": 3}
        for resources in ({}, [{"name": "coal", "tiles": 2}, {"name": "iron-ore", "tiles": 1}]):
            with self.subTest(resources=resources):
                detail = {"reason": "placement_rules", "mining_area_resources": resources}
                if not resources:
                    detail["reason_detail"] = "no_resources_in_mining_area"
                expected = {**detail, "mining_area_resources": resources or []}
                bridge = Mock()
                server = create_server(bridge)
                bridge.call.return_value = {"result": copy.deepcopy(detail)}
                checked = payload(await server.call_tool("check_placement", arguments))
                self.assertEqual(checked["mining_area_resources"], resources or [])
                bridge.call.side_effect = BridgeError("blocked", detail=copy.deepcopy(detail))
                placed = await server.call_tool("place_entity", arguments)
                self.assertTrue(placed.is_error)
                self.assertEqual(payload(placed)["detail"], expected)
                self.assertEqual(payload(placed)["hint"], HINTS["blocked"])

                bridge.call.side_effect = None
                bridge.call.return_value = {
                    "result": {
                        "results": [
                            {"index": 1, "ok": False, "error": "blocked", "detail": detail}
                        ],
                        "succeeded": 0,
                        "failed": 1,
                    }
                }
                batch = payload(await server.call_tool("place_entities", {"items": [arguments]}))
                self.assertEqual(batch["results"][0]["detail"], expected)

    async def test_non_drill_results_do_not_gain_resource_fields(self):
        bridge = Mock()
        bridge.call.return_value = {"result": {"placeable": True}}
        value = payload(
            await create_server(bridge).call_tool(
                "check_placement", {"item": "stone-furnace", "surface": "nauvis", "x": 2, "y": 3}
            )
        )
        self.assertNotIn("mining_area_resources", value)
        self.assertNotIn("reason_detail", value)

    async def test_drill_descriptions_and_hint_explain_resources(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for description in (
            tools["check_placement"].description,
            tools["place_entity"].description,
            tools["place_entities"].description,
            HINTS["blocked"],
        ):
            for text in ("mining_area_resources", "name, tiles", "no_resources_in_mining_area"):
                self.assertIn(text, description)


class TechnologyInfoTests(unittest.IsolatedAsyncioTestCase):
    async def test_locked_technology_details_are_forwarded(self):
        measured = {
            "name": "military-2",
            "researched": False,
            "available": False,
            "enabled": True,
            "prerequisites": [
                {"name": "logistic-science-pack", "researched": False},
                {"name": "military", "researched": True},
                {"name": "steel-processing", "researched": False},
            ],
            "unit_count": 20,
            "unit_time": 15,
            "ingredients": [
                {"name": "automation-science-pack", "amount": 1},
                {"name": "logistic-science-pack", "amount": 1},
            ],
            "unlocks_recipes": ["grenade", "piercing-rounds-magazine"],
        }
        bridge = Mock()
        bridge.call.return_value = {"tick": 123, "result": measured}
        result = await create_server(bridge).call_tool(
            "get_technology_info", {"technology": "military-2"}
        )
        self.assertFalse(result.is_error)
        self.assertEqual(payload(result), {"ok": True, "tick": 123, **measured})
        bridge.call.assert_called_once_with("get_technology_info", {"technology": "military-2"})

    async def test_trigger_technology_normalizes_empty_arrays(self):
        trigger = {"type": "craft-item", "item": "lab", "count": 1}
        for empty in ({}, None):
            with self.subTest(empty=empty):
                bridge = Mock()
                bridge.call.return_value = {
                    "result": {
                        "name": "automation-science-pack",
                        "prerequisites": empty,
                        "ingredients": empty,
                        "unlocks_recipes": empty,
                        "unit_count": 0,
                        "unit_time": 0,
                        "trigger": trigger,
                    }
                }
                result = await create_server(bridge).call_tool(
                    "get_technology_info", {"technology": "automation-science-pack"}
                )
                value = payload(result)
                for field in ("prerequisites", "ingredients", "unlocks_recipes"):
                    self.assertEqual(value[field], [])
                self.assertEqual(value["trigger"], trigger)
                self.assertEqual(value["unit_count"], 0)
                self.assertEqual(value["unit_time"], 0)

    async def test_unknown_technology_has_specific_hint(self):
        bridge = Mock()
        detail = {"technology": "unknown-tech", "reason": "unknown"}
        bridge.call.side_effect = BridgeError("invalid_target", detail=detail)
        result = await create_server(bridge).call_tool(
            "get_technology_info", {"technology": "unknown-tech"}
        )
        self.assertTrue(result.is_error)
        value = payload(result)
        self.assertEqual(value["error"], "invalid_target")
        self.assertEqual(value["detail"], detail)
        self.assertEqual(value["hint"], OPERATION_HINTS["get_technology_info", "invalid_target"])
        for text in ("detail.technology", "unknown", "force"):
            self.assertIn(text, value["hint"])

    async def test_schema_and_description(self):
        bridge = Mock()
        server = create_server(bridge)
        tools = {tool.name: tool for tool in await server.list_tools()}
        tool = tools["get_technology_info"]
        self.assertEqual(tool.input_schema["required"], ["technology"])
        self.assertEqual(tool.input_schema["properties"]["technology"]["minLength"], 1)
        for text in ("force", "Read-only", "prerequisites", "unit_time in seconds", "trigger"):
            self.assertIn(text, tool.description)
        with self.assertRaises(ToolError):
            await server.call_tool("get_technology_info", {"technology": ""})
        bridge.call.assert_not_called()


class ResearchStallTests(unittest.IsolatedAsyncioTestCase):
    async def wait_with_clock(self, status_at, timeout=20):
        elapsed = 0.0

        async def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        bridge = Mock()
        bridge.call.side_effect = lambda *_: {"result": status_at(elapsed)}
        server = create_server(bridge)
        with (
            patch("factorio_mcp.server.time") as clock,
            patch("factorio_mcp.server.asyncio.sleep", side_effect=advance) as sleep,
        ):
            clock.monotonic.side_effect = lambda: elapsed
            result = await server.call_tool(
                "wait_for_research", {"technology": "automation", "timeout_seconds": timeout}
            )
        self.assertFalse(result.is_error)
        return payload(result), elapsed, sleep.await_count

    async def test_constant_progress_stalls_after_ten_simulated_seconds(self):
        for progress in (0, 0.8):
            with self.subTest(progress=progress):
                value, elapsed, sleeps = await self.wait_with_clock(
                    lambda _, progress=progress: {
                        "current": "automation",
                        "progress": progress,
                        "remaining_units": 10 * (1 - progress),
                    }
                )
                self.assertTrue(value["stalled"])
                self.assertFalse(value["timed_out"])
                self.assertFalse(value["researched"])
                self.assertTrue(value["researching"])
                self.assertEqual(value["progress"], progress)
                self.assertEqual(value["remaining_units"], 10 * (1 - progress))
                self.assertEqual(value["stalled_seconds"], 10)
                self.assertEqual(value["waited_seconds"], elapsed)
                self.assertEqual(elapsed, 10)
                self.assertEqual(sleeps, 40)
                for text in (
                    "missing_science",
                    "observe_nearby",
                    "power",
                    "every required",
                    "every lab with packs",
                    "For each ingredient",
                    "sum of science_units",
                    "exceeds remaining_units",
                    "Partly used packs stay in the lab",
                    "labs working in parallel each need a pack of every ingredient",
                ):
                    self.assertIn(text, value["hint"])

    async def test_progress_change_resets_stall_timer(self):
        value, elapsed, _ = await self.wait_with_clock(
            lambda t: {"current": "automation", "progress": 0.8 if t < 8 else 0.81}
        )
        self.assertTrue(value["stalled"])
        self.assertEqual(elapsed, 18)
        self.assertEqual(value["stalled_seconds"], 10)
        self.assertEqual(value["progress"], 0.81)

    async def test_queue_wait_and_time_on_other_research_do_not_count_as_stalled(self):
        for initially_current in (False, True):
            with self.subTest(initially_current=initially_current):

                def status_at(t, initially_current=initially_current):
                    current = "automation" if t >= 8 or (initially_current and t < 4) else "other"
                    return {"current": current, "progress": 0.8, "queue": ["automation"]}

                value, elapsed, _ = await self.wait_with_clock(status_at)
                self.assertTrue(value["stalled"])
                self.assertEqual(elapsed, 18)
                self.assertEqual(value["stalled_seconds"], 10)

    async def test_timeout_without_stall_for_queued_missing_or_changing_progress(self):
        for status_at in (
            lambda _: {"current": "other", "progress": 0.8, "queue": ["automation"]},
            lambda _: {"current": "automation"},
            lambda t: {"current": "automation", "progress": 0.5 + t * 1e-12},
        ):
            with self.subTest(status_at=status_at):
                value, elapsed, _ = await self.wait_with_clock(status_at)
                self.assertFalse(value["stalled"])
                self.assertTrue(value["timed_out"])
                self.assertEqual(elapsed, 20)
                self.assertNotIn("stalled_seconds", value)
                self.assertNotIn("hint", value)

    async def test_completion_and_removal_take_precedence_at_stall_threshold(self):
        for final in ({"researched": ["automation"]}, {}):
            with self.subTest(final=final):
                value, elapsed, _ = await self.wait_with_clock(
                    lambda t, final=final: (
                        {"current": "automation", "progress": 0.8} if t < 10 else final
                    )
                )
                self.assertFalse(value["stalled"])
                self.assertFalse(value["timed_out"])
                self.assertEqual(value["researched"], bool(final))
                self.assertNotIn("remaining_units", value)
                self.assertEqual(elapsed, 10)

    async def test_short_timeout_returns_without_stall(self):
        value, elapsed, _ = await self.wait_with_clock(
            lambda t: {
                "current": "automation",
                "progress": 0.8 + t / 100,
                "remaining_units": 2 - t / 10,
            },
            timeout=5,
        )
        self.assertTrue(value["timed_out"])
        self.assertFalse(value["stalled"])
        self.assertEqual(elapsed, 5)
        self.assertEqual(value["remaining_units"], 1.5)

    async def test_other_current_research_does_not_supply_remaining_units(self):
        value, _, _ = await self.wait_with_clock(
            lambda _: {
                "current": "other",
                "progress": 0.8,
                "remaining_units": 2,
                "queue": ["automation"],
            },
            timeout=1,
        )
        self.assertTrue(value["timed_out"])
        self.assertNotIn("remaining_units", value)

    async def test_zero_remaining_units_is_forwarded(self):
        value, _, _ = await self.wait_with_clock(
            lambda _: {"current": "automation", "progress": 1, "remaining_units": 0}, timeout=1
        )
        self.assertEqual(value["remaining_units"], 0)

    async def test_description_explains_stall_detection_and_diagnostics(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for text in (
            "10 seconds",
            "stalled=true",
            "stalled_seconds",
            "timed_out=false",
            "missing_science",
            "power",
            "every required science pack",
            "remaining_units",
            "sum of science_units",
            "exceeds remaining_units",
        ):
            self.assertIn(text, tools["wait_for_research"].description)


if __name__ == "__main__":
    unittest.main()
