import copy
import json
import unittest
from unittest.mock import Mock, patch

from factorio_mcp.bridge import BridgeError
from factorio_mcp.server import create_server


def payload(response):
    return json.loads(response.content[0].text)


MACHINES = ("assembling-machine-2", "oil-refinery", "chemical-plant")
PLACEABLES = (*MACHINES, "pumpjack", "storage-tank", "pump", "steel-chest")
FLUIDS = [{"index": 1, "name": "crude-oil", "amount": 12.5, "capacity": 1000, "temperature": 25}]
PORTS = [
    {
        "position": {"x": 1.5, "y": -0.5},
        "target_position": {"x": 2.5, "y": -0.5},
        "direction": 4,
        "flow": "output",
        "fluidbox_index": 1,
        "connected": False,
    }
]
OIL = {"name": "crude-oil", "tiles": 2, "count": 2, "yield_percent": 150}


class OilEntityTests(unittest.IsolatedAsyncioTestCase):
    async def test_descriptions_cover_oil_entities_and_resource_semantics(self):
        tools = {tool.name: tool for tool in await create_server(None).list_tools()}
        for operation in ("place_entity", "check_placement"):
            for entity in PLACEABLES:
                self.assertIn(entity, tools[operation].description)
            for phrase in ("fluid_connections", "no_resource", "crude oil", "live API"):
                self.assertIn(phrase, tools[operation].description)
        for operation in ("get_entity_inventory", "get_entity_inventories"):
            for phrase in (*PLACEABLES, "source/result", "mining_target", "fluids"):
                self.assertIn(phrase, tools[operation].description)
        for operation in ("observe_nearby", "get_explored_map"):
            for phrase in ("crude-oil", "count", "yield_percent", "normal_resource_amount"):
                self.assertIn(phrase, tools[operation].description)
        for phrase in (
            *MACHINES,
            "basic-oil-processing",
            "plastic-bar",
            "unsupported_recipe_category",
        ):
            self.assertIn(phrase, tools["set_recipe"].description)
        for operation in ("transfer_item", "wait_for_items"):
            for entity in MACHINES:
                self.assertIn(entity, tools[operation].description)
        for operation in ("get_technology_info", "get_research_status"):
            for phrase in ("trigger", "crude-oil", "pumpjack"):
                self.assertIn(phrase, tools[operation].description)
        self.assertIn("fluid_resource", tools["mine_entity"].description)

    async def test_oil_inventory_fields_survive_single_and_batch_reads(self):
        bridge = Mock()
        server = create_server(bridge)
        for entity in PLACEABLES:
            value = {
                "name": entity,
                "type": "assembling-machine" if entity in MACHINES else entity,
                "inventories": {"source": {}, "result": {}} if entity in MACHINES else {},
                "status": "no_power",
            }
            if entity != "steel-chest":
                value["fluids"] = copy.deepcopy(FLUIDS)
            else:
                value["inventories"] = {"main": [{"name": "coal", "count": 2}]}
            if entity == "pumpjack":
                value["mining_target"] = "crude-oil"
            if entity in MACHINES:
                value.update(
                    recipe="plastic-bar", missing_ingredients=[{"name": "coal", "count": 1}]
                )
            for operation, args, measured in (
                ("get_entity_inventory", {"target_ref": "oil"}, value),
                (
                    "get_entity_inventories",
                    {"target_refs": ["oil"]},
                    {"results": [{"index": 1, "ok": True, "result": value}]},
                ),
            ):
                with self.subTest(entity=entity, operation=operation):
                    bridge.call.return_value = {"result": copy.deepcopy(measured)}
                    response = await server.call_tool(operation, args)
                    self.assertFalse(response.is_error, payload(response))
                    result = payload(response)
                    if operation == "get_entity_inventories":
                        result = result["results"][0]["result"]
                    for field in ("fluids", "mining_target"):
                        if field in value:
                            self.assertEqual(result[field], value[field])
                        else:
                            self.assertNotIn(field, result)
                    self.assertEqual(result["status"], value["status"])
                    if entity in MACHINES:
                        self.assertEqual(result["inventories"], {"source": [], "result": []})
                        self.assertEqual(result["recipe"], "plastic-bar")
                        self.assertEqual(
                            result["missing_ingredients"], value["missing_ingredients"]
                        )

    async def test_crude_oil_count_yield_and_partial_map_passthrough(self):
        bridge = Mock()
        server = create_server(bridge)
        for compact in (False, True):
            observed = {"target_ref": "jack", "name": "pumpjack", "mining_target": "crude-oil"}
            if not compact:
                observed["fluids"] = FLUIDS
            bridge.call.return_value = {"result": {"entities": [observed], "resources": [OIL]}}
            result = payload(await server.call_tool("observe_nearby", {"compact": compact}))
            self.assertEqual(result["resources"], [OIL])
            self.assertNotIn("total_amount", result["resources"][0])
            if compact:
                self.assertNotIn("fluids", result["entities"][0])
            else:
                self.assertEqual(result["entities"][0]["fluids"], FLUIDS)
            self.assertEqual(result["entities"][0]["mining_target"], "crude-oil")
        partial = {**OIL, "partial": True, "observed_tick": 123, "cells": 1}
        bridge.call.return_value = {"result": {"resources": [partial], "explored": {"cells": 0}}}
        result = payload(
            await server.call_tool("get_explored_map", {"resource_names": ["crude-oil"]})
        )
        self.assertEqual(result["resources"], [partial])
        self.assertEqual(result["explored"], {"cells": 0})

    async def test_set_recipe_passthrough_and_category_rejection(self):
        bridge = Mock()
        server = create_server(bridge)
        for machine, recipe in (
            ("assembling-machine-2", "electric-engine-unit"),
            ("oil-refinery", "basic-oil-processing"),
            ("chemical-plant", "plastic-bar"),
            ("chemical-plant", "sulfur"),
            ("chemical-plant", "lubricant"),
        ):
            with self.subTest(machine=machine, recipe=recipe):
                args = {"target_ref": machine, "recipe": recipe}
                bridge.call.return_value = {"result": {"name": machine, "recipe": recipe}}
                result = payload(await server.call_tool("set_recipe", args))
                bridge.call.assert_called_with("set_recipe", args)
                self.assertEqual(result["recipe"], recipe)
        detail = {
            "reason": "unsupported_recipe_category",
            "entity": "assembling-machine-1",
            "recipe": "plastic-bar",
            "category": "chemistry",
            "supported_categories": ["crafting"],
            "hint": "Choose a machine that supports this recipe category.",
        }
        bridge.call.side_effect = BridgeError("invalid_input", detail=detail)
        response = await server.call_tool(
            "set_recipe", {"target_ref": "old", "recipe": "plastic-bar"}
        )
        self.assertTrue(response.is_error)
        self.assertEqual(payload(response)["detail"], detail)
        self.assertEqual(payload(response)["hint"], detail["hint"])

    async def test_placement_ports_and_off_oil_reason_passthrough(self):
        bridge = Mock()
        server = create_server(bridge)
        for entity in PLACEABLES:
            for operation in ("check_placement", "place_entity"):
                value = {
                    "name": entity,
                    "fluid_connections": PORTS,
                    "mining_area_resources": [{"name": "crude-oil", "tiles": 1}],
                }
                bridge.call.return_value = {"result": copy.deepcopy(value)}
                response = await server.call_tool(
                    operation,
                    {"item": entity, "surface": "nauvis", "x": 0.5, "y": 0.5, "direction": 4},
                )
                result = payload(response)
                self.assertFalse(response.is_error, result)
                self.assertEqual(result["fluid_connections"], PORTS)
                self.assertEqual(result["mining_area_resources"], value["mining_area_resources"])
        blocked = {
            "placeable": False,
            "blocked_reason": "no_resource",
            "mining_area_resources": {},
            "hint": "Pumpjacks must be placed on crude oil.",
        }
        bridge.call.return_value = {"result": blocked}
        result = payload(
            await server.call_tool(
                "check_placement", {"item": "pumpjack", "surface": "nauvis", "x": 0, "y": 0}
            )
        )
        self.assertEqual(result["blocked_reason"], "no_resource")
        self.assertEqual(result["mining_area_resources"], [])
        detail = {"reason": "fluid_resource", "entity": "crude-oil"}
        bridge.call.side_effect = BridgeError("invalid_target", detail=detail)
        response = await server.call_tool("mine_entity", {"target_ref": "oil"})
        self.assertTrue(response.is_error)
        self.assertEqual(payload(response)["detail"], detail)
        self.assertIn("pumpjack", payload(response)["hint"])

    async def test_new_machine_wait_aliases_and_production_counts(self):
        for machine in MACHINES:
            for produced in (False, True):
                with self.subTest(machine=machine, produced=produced):
                    clock_state, reads = [0.0], 0

                    async def advance(seconds, state=clock_state):
                        state[0] += seconds

                    def respond(operation, params, machine=machine):
                        nonlocal reads
                        if operation == "get_character_position":
                            return {"result": {"position": {"x": 0, "y": 0}}}
                        self.assertEqual(operation, "get_entity_inventory")
                        reads += 1
                        return {
                            "result": {
                                "name": machine,
                                "type": "assembling-machine",
                                "status": "working",
                                "inventories": {
                                    "source": [],
                                    "result": [{"name": "plastic-bar", "count": 2}],
                                },
                                "products_finished": 10 + reads,
                                "recipe_products": ["plastic-bar"],
                                "recipe_product_amounts": {"plastic-bar": 2},
                            }
                        }

                    bridge = Mock()
                    bridge.call.side_effect = respond
                    with (
                        patch("factorio_mcp.server.time") as clock,
                        patch("factorio_mcp.server.asyncio.sleep", side_effect=advance),
                    ):
                        clock.monotonic.side_effect = lambda state=clock_state: state[0]
                        response = await create_server(bridge).call_tool(
                            "wait_for_items",
                            {
                                "target_ref": "machine",
                                "item": "plastic-bar",
                                "count": 2,
                                "inventory_role": "output",
                                "produced": produced,
                                "timeout_seconds": 1,
                            },
                        )
                    self.assertFalse(response.is_error, payload(response))
                    self.assertTrue(payload(response)["reached"])
                    self.assertEqual(payload(response)["inventory_role"], "result")
                    self.assertEqual(payload(response)["current"], 2)
                    self.assertEqual(reads, 2 if produced else 1)

    async def test_transfers_and_placement_errors_keep_mod_roles_and_hints(self):
        bridge = Mock()
        server = create_server(bridge)
        for machine, role in (*((m, "input") for m in MACHINES), ("steel-chest", "main")):
            args = {
                "target_ref": machine,
                "direction": "to_entity",
                "inventory_role": role,
                "item": "coal",
                "count": 2,
            }
            measured = {
                "moved": 2,
                "requested": 2,
                "inventory_role": "main" if role == "main" else "source",
            }
            bridge.call.return_value = {"result": measured}
            result = payload(await server.call_tool("transfer_item", args))
            bridge.call.assert_called_with("transfer_item", args)
            self.assertEqual(result["inventory_role"], measured["inventory_role"])
            bridge.call.return_value = {
                "result": {"results": [{"index": 1, "ok": True, "result": measured}]}
            }
            result = payload(await server.call_tool("transfer_items", {"items": [args]}))
            self.assertEqual(result["results"][0]["result"], measured)
        detail = {
            "reason": "no_resource",
            "mining_area_resources": {},
            "hint": "Pumpjacks must be placed on crude oil.",
        }
        args = {"item": "pumpjack", "surface": "nauvis", "x": 0, "y": 0}
        bridge.call.side_effect = BridgeError("blocked", detail=copy.deepcopy(detail))
        response = await server.call_tool("place_entity", args)
        self.assertTrue(response.is_error)
        self.assertEqual(payload(response)["hint"], detail["hint"])
        self.assertEqual(payload(response)["detail"]["mining_area_resources"], [])
        bridge.call.side_effect = None
        bridge.call.return_value = {
            "result": {"results": [{"index": 1, "ok": False, "error": "blocked", "detail": detail}]}
        }
        result = payload(await server.call_tool("place_entities", {"items": [args]}))
        self.assertEqual(result["results"][0]["hint"], detail["hint"])

    async def test_hidden_live_fluid_boxes_are_not_synthesized(self):
        bridge = Mock()
        bridge.call.return_value = {"result": {"name": "assembling-machine-2"}}
        response = await create_server(bridge).call_tool(
            "place_entity", {"item": "assembling-machine-2", "surface": "nauvis", "x": 0, "y": 0}
        )
        self.assertNotIn("fluid_connections", payload(response))
        self.assertNotIn("fluids", payload(response))

    async def test_mining_research_trigger_entity_passthrough(self):
        trigger = {"type": "mine-entity", "entity": "crude-oil"}
        bridge = Mock()
        bridge.call.return_value = {"result": {"name": "oil-processing", "trigger": trigger}}
        server = create_server(bridge)
        result = payload(
            await server.call_tool("get_technology_info", {"technology": "oil-processing"})
        )
        self.assertEqual(result["trigger"], trigger)
        bridge.call.return_value = {
            "result": {"available": [{"name": "oil-processing", "trigger": trigger}]}
        }
        result = payload(await server.call_tool("get_research_status", {}))
        self.assertEqual(result["available"][0]["trigger"], trigger)
