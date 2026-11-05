import json
import unittest
from unittest.mock import Mock

from factorio_mcp.server import create_server


def payload(result):
    return json.loads(result.content[0].text)


class TransferResearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_transfer_details_survive_single_and_batch_results(self):
        arguments = {
            "target_ref": "furnace",
            "direction": "to_entity",
            "inventory_role": "fuel",
            "item": "coal",
            "count": 80,
        }
        for result in (
            {
                "moved": 18,
                "requested": 80,
                "partial": True,
                "limited_by": "source",
                "available": 18,
                "insertable": 50,
            },
            {
                "moved": 50,
                "requested": 80,
                "partial": True,
                "limited_by": "destination",
                "available": 80,
                "insertable": 50,
            },
            {"moved": 80, "requested": 80},
        ):
            with self.subTest(result=result):
                bridge = Mock()
                server = create_server(bridge)
                bridge.call.return_value = {"result": result}
                single = await server.call_tool("transfer_item", arguments)
                self.assertFalse(single.is_error)
                self.assertEqual({k: payload(single)[k] for k in result}, result)
                if "partial" not in result:
                    for field in ("partial", "limited_by", "available", "insertable"):
                        self.assertNotIn(field, payload(single))
                batch = {
                    "results": [{"index": 1, "ok": True, "result": result}],
                    "succeeded": 1,
                    "failed": 0,
                }
                bridge.call.return_value = {"result": batch}
                response = await server.call_tool("transfer_items", {"items": [arguments]})
                self.assertFalse(response.is_error)
                self.assertEqual(payload(response)["results"], batch["results"])

    async def test_research_status_preserves_remaining_units_or_omission(self):
        for status in ({}, {"current": "automation", "progress": 0.983, "remaining_units": 0.17}):
            with self.subTest(status=status):
                bridge = Mock()
                bridge.call.return_value = {"result": status}
                response = await create_server(bridge).call_tool("get_research_status", {})
                self.assertFalse(response.is_error)
                value = payload(response)
                if "current" in status:
                    self.assertEqual(value["remaining_units"], 0.17)
                else:
                    self.assertNotIn("remaining_units", value)

    async def test_lab_science_units_preserve_fractions_and_omission(self):
        entities = [
            {
                "name": "lab",
                "science_units": [
                    {"name": "automation-science-pack", "units": 1.234},
                    {"name": "logistic-science-pack", "units": 2},
                ],
                "missing_science": ["chemical-science-pack"],
            },
            {"name": "lab"},
        ]
        bridge = Mock()
        bridge.call.return_value = {"result": {"entities": entities}}
        response = await create_server(bridge).call_tool("observe_nearby", {})
        self.assertFalse(response.is_error)
        self.assertEqual(payload(response)["entities"], entities)

    async def test_descriptions_explain_partial_transfers_and_remaining_science(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for name in ("transfer_item", "transfer_items"):
            for text in (
                "requested",
                "partial=true",
                "limited_by",
                "source",
                "destination",
                "available",
                "insertable",
                "before the move",
                "prefer source",
                "omit",
            ):
                self.assertIn(text, tools[name].description)
        for text in (
            "science_units",
            "{name, units}",
            "sorted by name",
            "partly used",
            "3 decimals",
            "full pack is 1",
            "input is empty",
            "missing_science",
        ):
            self.assertIn(text, tools["observe_nearby"].description)
        for text in (
            "remaining_units",
            "unit_count * (1 - progress)",
            "force",
            "count-formula",
            "omitted when nothing",
        ):
            self.assertIn(text, tools["get_research_status"].description)


if __name__ == "__main__":
    unittest.main()
