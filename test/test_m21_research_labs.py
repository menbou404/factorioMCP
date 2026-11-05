import copy
import json
import unittest
from unittest.mock import Mock, call, patch

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import _normalize_result, create_server

TECHNOLOGY = "military-2"
REMAINING = 0.00043
RED = "automation-science-pack"
GREEN = "logistic-science-pack"


def payload(result):
    return json.loads(result.content[0].text)


class ResearchLabsTests(unittest.IsolatedAsyncioTestCase):
    async def wait_with_clock(self, diagnostics, status_at=None, timeout=20):
        elapsed = 0.0

        async def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        def read(operation, params):
            self.assertIsNone(params)
            if operation == "get_research_status":
                status = (
                    status_at(elapsed)
                    if status_at
                    else {
                        "current": TECHNOLOGY,
                        "progress": 0.9999785,
                        "remaining_units": REMAINING,
                    }
                )
                return {"result": status}
            self.assertEqual(operation, "research_labs")
            if isinstance(diagnostics, Exception):
                raise diagnostics
            return {"result": copy.deepcopy(diagnostics)}

        bridge = Mock()
        bridge.call.side_effect = read
        with (
            patch("factorio_mcp.server.time") as clock,
            patch("factorio_mcp.server.asyncio.sleep", side_effect=advance),
        ):
            clock.monotonic.side_effect = lambda: elapsed
            result = await create_server(bridge).call_tool(
                "wait_for_research", {"technology": TECHNOLOGY, "timeout_seconds": timeout}
            )
        self.assertFalse(result.is_error)
        return payload(result), bridge

    async def test_stalled_result_includes_lab_diagnosis_without_rounding(self):
        diagnostics = {
            "technology": TECHNOLOGY,
            "remaining_units": REMAINING,
            "labs": [
                {
                    "target_ref": "42",
                    "x": 2.5,
                    "y": 1.5,
                    "status": "missing_science_packs",
                    "science_units": [{"name": RED, "units": 0.00043123}],
                    "missing": [GREEN],
                    "blocking": True,
                },
                {
                    "target_ref": "43",
                    "x": 6.5,
                    "y": 1.5,
                    "status": "no_power",
                    "science_units": [{"name": RED, "units": 2}, {"name": GREEN, "units": 0.00042}],
                    "missing": [],
                    "blocking": False,
                },
            ],
            "short_science": [
                {"name": GREEN, "available_units": 0.00042, "remaining_units": REMAINING}
            ],
        }
        value, bridge = await self.wait_with_clock(diagnostics)
        self.assertTrue(value["stalled"])
        self.assertFalse(value["timed_out"])
        self.assertFalse(value["researched"])
        self.assertTrue(value["researching"])
        self.assertEqual(value["stalled_seconds"], 10)
        self.assertEqual(value["remaining_units"], REMAINING)
        self.assertEqual(value["labs"], diagnostics["labs"])
        self.assertEqual(value["short_science"], diagnostics["short_science"])
        self.assertNotIn("labs_error", value)
        self.assertEqual(bridge.call.call_args_list[-1], call("research_labs", None))
        self.assertEqual(
            sum(entry == call("research_labs", None) for entry in bridge.call.call_args_list), 1
        )
        for text in ("blocking=true", "take packs out", "missing", "Add packs", "short_science"):
            self.assertIn(text, value["hint"])

    async def test_diagnostic_failure_preserves_stalled_result(self):
        for error, code in (
            (BridgeError("not_connected", "RCON unavailable"), "not_connected"),
            (RuntimeError("bad diagnostic response"), "internal_error"),
        ):
            with self.subTest(code=code):
                value, _ = await self.wait_with_clock(error)
                self.assertTrue(value["stalled"])
                self.assertTrue(value["researching"])
                self.assertFalse(value["timed_out"])
                self.assertEqual(value["remaining_units"], REMAINING)
                self.assertEqual(value["progress"], 0.9999785)
                self.assertEqual(value["stalled_seconds"], 10)
                self.assertEqual(value["labs_error"], {"error": code, "message": str(error)})
                self.assertNotIn("labs", value)
                self.assertIn("blocking=true", value["hint"])

    async def test_no_labs_normalizes_lua_empty_tables_and_suggests_moving(self):
        value, _ = await self.wait_with_clock(
            {"technology": TECHNOLOGY, "labs": {}, "short_science": {}}
        )
        self.assertTrue(value["stalled"])
        self.assertEqual(value["labs"], [])
        self.assertEqual(value["short_science"], [])
        self.assertIn("Stand near the labs and wait again", value["hint"])
        self.assertIn("16 tiles", value["hint"])

    async def test_changed_current_research_keeps_stall_without_wrong_lab_diagnosis(self):
        for technology in (None, "other-research"):
            with self.subTest(technology=technology):
                value, _ = await self.wait_with_clock(
                    {"technology": technology, "labs": [], "short_science": []}
                )
                self.assertTrue(value["stalled"])
                self.assertEqual(value["technology"], TECHNOLOGY)
                self.assertEqual(value["labs_error"]["error"], "research_changed")
                self.assertNotIn("labs", value)

    async def test_non_stalled_results_never_request_lab_diagnostics(self):
        for status_at, timeout in (
            (lambda _: {"researched": [TECHNOLOGY]}, 20),
            (lambda _: {}, 20),
            (lambda _: {"current": TECHNOLOGY, "progress": 0.5}, 1),
            (lambda _: {"current": "other", "queue": [TECHNOLOGY], "progress": 0.5}, 20),
            (lambda t: {"current": TECHNOLOGY, "progress": t / 100}, 20),
        ):
            with self.subTest(status_at=status_at):
                value, bridge = await self.wait_with_clock(None, status_at, timeout)
                self.assertFalse(value["stalled"])
                self.assertNotIn("labs", value)
                self.assertNotIn("short_science", value)
                self.assertNotIn("labs_error", value)
                self.assertTrue(
                    all(
                        entry.args == ("get_research_status", None)
                        for entry in bridge.call.call_args_list
                    )
                )

    def test_nested_lua_empty_tables_are_normalized(self):
        result = _normalize_result(
            "research_labs",
            {
                "labs": [{"science_units": {}, "missing": {}, "blocking": False}],
                "short_science": {},
            },
        )
        self.assertEqual(
            result,
            {
                "labs": [{"science_units": [], "missing": [], "blocking": False}],
                "short_science": [],
            },
        )

    async def test_description_explains_scope_and_diagnostic_fields(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for text in (
            "16 tiles",
            "character's surface",
            "stable target_ref",
            "status",
            "unpowered",
            "unrounded science_units",
            "missing",
            "blocking",
            "short_science",
            "available_units <= remaining_units",
            "only labs holding every required ingredient",
            "labs_error",
            "stand near the labs and wait again",
        ):
            self.assertIn(text, tools["wait_for_research"].description)


if __name__ == "__main__":
    unittest.main()
