import copy
import json
import re
import unittest
from pathlib import Path
from unittest.mock import Mock

from factorio_mcp.server import _normalize_result, create_server

CONTROL = (Path(__file__).resolve().parents[1] / "mod/factorio_ai_mcp_0.1.0/control.lua").read_text(
    encoding="utf-8"
)


def lua_function(name):
    match = re.search(rf"local function {name}\(.*?\nend", CONTROL, re.S)
    assert match is not None, name
    return match[0]


class TypesBuildingsStandTests(unittest.TestCase):
    def test_placement_uses_place_result_type_and_retains_checks(self):
        validate = lua_function("validate_placement")
        self.assertIn("prototype and PLACEABLE_TYPES[prototype.type]", validate)
        for text in (
            "PLACEABLE_ITEMS[params.item]",
            "character.build_distance",
            "placement_visible",
            "placement_fluid_mixing",
            "character.can_place_entity",
            "character.surface.can_place_entity",
            "missing_materials",
        ):
            self.assertIn(text, validate)
        self.assertIn("PLACEABLE_TYPES[prototype.type]", lua_function("do_check_placement"))
        self.assertIn(
            'place_result.type == "underground-belt"', lua_function("valid_underground_type")
        )
        line = lua_function("validate_line")
        self.assertIn('prototype.type ~= "transport-belt" and prototype.type ~= "pipe"', line)

    def test_roles_and_aliases_keep_name_overrides_and_use_burners(self):
        roles = lua_function("entity_inventory_roles")
        self.assertLess(roles.index("INVENTORY_ROLES[entity.name]"), roles.index("entity.type"))
        for kind in (
            "container",
            "furnace",
            "assembling-machine",
            "lab",
            "boiler",
            "mining-drill",
            "inserter",
        ):
            self.assertIn(f'"{kind}"', roles)
        self.assertIn('kind == "furnace" and entity.burner', roles)
        self.assertIn('kind == "inserter") and entity.burner', roles)
        self.assertIn("ROLE_ALIASES[entity.name]", lua_function("entity_role_aliases"))
        self.assertIn("entity_inventory_roles(entity)", lua_function("do_observe"))
        self.assertIn("entity_inventory_roles(target)", lua_function("get_entity_inventory"))
        self.assertIn("STATUS_ONLY_TYPES[target.type]", lua_function("get_entity_inventory"))
        self.assertIn("entity_role_aliases(target)", lua_function("do_transfer"))

    def test_inserter_diagnostics_and_settings_are_type_based(self):
        for name in ("add_item_targets", "preview_inserter_positions", "do_observe"):
            body = lua_function(name)
            self.assertIn('"inserter"', body)
            self.assertNotIn('name == "inserter"', body)
            self.assertNotIn('name == "long-handed-inserter"', body)
        self.assertIn("target.rotate{", lua_function("do_rotate"))
        self.assertIn(
            'settings_target(state, params, "inserter")', lua_function("do_set_inserter_filter")
        )

    def test_buildings_use_only_observed_entities_and_full_cells_replace(self):
        record = lua_function("record_explored_map")
        self.assertNotIn("find_entities", record)
        self.assertIn("entity.force == character.force", record)
        self.assertIn("distance_squared(character.position, position) <= radius * radius", record)
        for kind in (
            "inserter",
            "transport-belt",
            "underground-belt",
            "splitter",
            "pipe",
            "pipe-to-ground",
            "electric-pole",
        ):
            self.assertIn(f'entity.type ~= "{kind}"', record)
        for text in (
            "buildings = {}",
            "building.count = building.count + 1",
            "nearest_sample(building.position, position, centre)",
            "sorted_keys(cell.buildings)",
            "previous.buildings or {}",
            "math.max(building.count, old and old.count or 0)",
            "map.cells[key] = cell",
        ):
            self.assertIn(text, record)

    def test_building_summary_covers_all_cells_before_limit_and_old_saves(self):
        body = lua_function("do_get_explored_map")
        self.assertIn("local buildings = cell.buildings or {}", body)
        self.assertIn("pairs(map.partial_cells or {})", body)
        self.assertIn("pairs(map.cells)", body)
        self.assertNotIn("result.cells", body)
        self.assertLess(
            body.index("local summary = {}"),
            body.index("for _, cell in pairs(resource_cells)", body.index("local summary = {}")),
        )
        self.assertLess(
            body.index("sorted_keys(summary)"),
            body.index("limit + 1, -1 do result.building_cells"),
        )
        self.assertIn("entry.count + building.count, entry.cells + 1", body)
        self.assertIn("nearest_sample(entry.nearest, building.position, character.position)", body)
        self.assertIn("distance_squared(character.position, entry.nearest)", body)
        self.assertIn("if #buildings > 0 then", body)
        self.assertIn("table.sort(result.building_cells", body)
        self.assertIn("result.building_cells_total = #result.building_cells", body)

    def test_corridor_fallback_runs_last_and_reuses_collision_rules(self):
        body = lua_function("line_stand_position")
        fallback = body[body.index("if not best then") :]
        for text in (
            "character.build_distance - 1",
            "next_position.x + fallback_reach, 0.5",
            "next_position.y + fallback_reach, 0.5",
            "for index = next_index, line.requested - 1",
            "if not remaining then",
            "table.sort(candidates",
            "distance_squared(a, character.position)",
            "consider(candidate)",
            "if best then break end",
        ):
            self.assertIn(text, fallback)
        self.assertIn("placement_stand_clear(state, character, candidate)", body)
        self.assertIn("entity.valid and entity ~= character then return end", body)


class TypesBuildingsServerTests(unittest.IsolatedAsyncioTestCase):
    def test_empty_building_arrays_and_old_map_responses(self):
        empty = _normalize_result(
            "get_explored_map", {"building_cells": {}, "building_summary": {}}
        )
        self.assertEqual(empty["building_cells"], [])
        self.assertEqual(empty["building_summary"], [])
        cells = _normalize_result(
            "get_explored_map",
            {"building_cells": [{"cx": 0, "cy": 0, "buildings": {}}], "building_summary": {}},
        )
        self.assertEqual(cells["building_cells"][0]["buildings"], [])
        old = _normalize_result("get_explored_map", {"explored": {"cells": 1}})
        self.assertNotIn("cells", old)
        self.assertNotIn("building_cells", old)
        self.assertNotIn("building_summary", old)

    async def test_descriptions_and_upgraded_underground_forwarding(self):
        bridge = Mock()
        bridge.call.return_value = {"result": {}}
        server = create_server(bridge)
        tools = {tool.name: tool for tool in await server.list_tools()}
        for name in ("place_entity", "place_entities", "check_placement", "place_line"):
            self.assertIn("other entities of these types", tools[name].description)
        for text in (
            "building_cells_total",
            "building_summary={name,count,cells,nearest={x,y},distance}",
            "ALL remembered full and partial cells before limiting",
        ):
            self.assertIn(text, tools["get_explored_map"].description)
        for name in ("place_entity", "check_placement"):
            params = {
                "item": "fast-underground-belt",
                "surface": "nauvis",
                "x": 1,
                "y": 2,
                "direction": 4,
                "underground_type": "output",
            }
            response = await server.call_tool(name, params)
            self.assertFalse(response.is_error)
            bridge.call.assert_called_with(name, params)

    async def test_building_cells_and_global_summary_pass_through(self):
        measured = {
            "building_cells": [
                {
                    "cx": 0,
                    "cy": 0,
                    "position": {"x": 4, "y": 4},
                    "observed_tick": 123,
                    "partial": True,
                    "distance": 4,
                    "buildings": [
                        {"name": "steel-furnace", "count": 2, "position": {"x": 3, "y": 4}}
                    ],
                }
            ],
            "building_cells_total": 12,
            "building_summary": [
                {
                    "name": "oil-refinery",
                    "count": 1,
                    "cells": 1,
                    "nearest": {"x": 800, "y": 4},
                    "distance": 800,
                },
                {
                    "name": "steel-furnace",
                    "count": 5,
                    "cells": 3,
                    "nearest": {"x": 3, "y": 4},
                    "distance": 3,
                },
            ],
        }
        bridge = Mock()
        bridge.call.return_value = {"result": copy.deepcopy(measured)}
        response = await create_server(bridge).call_tool("get_explored_map", {"limit": 1})
        value = json.loads(response.content[0].text)
        for key, expected in measured.items():
            self.assertEqual(value[key], expected)


if __name__ == "__main__":
    unittest.main()
