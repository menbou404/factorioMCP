"""Automated green science on the disposable RCON 27277 save, after automation_line.py.

Inherits its character, inventory, smelters, red-science line and steam network.
Only research uses /c. Items come from mining, smelting and normal tool crafting.
Never run against a real save.
"""

import asyncio
import json
import math
import time

import power_research as power
from automation_line import orient_inserter, restore_pairs
from helpers import (
    assert_inserter_direction,
    position_in_tile_footprint,
    successful_batch_results,
)
from power_research import (
    BOILER,
    LAB,
    Recipes,
    amount,
    bridge,
    call,
    entity_at,
    gather,
    items,
    observe,
    top_up,
    walk,
)
from rcon.source import Client

from factorio_mcp.bridge import RCON_FRAG_THRESHOLD, BridgeError
from factorio_mcp.server import create_server
from production_line import action as shared_action
from production_line import password

SCIENCE = "logistic-science-pack"
ASSEMBLERS = {
    "iron-gear-wheel": (5.5, -14.5),
    "inserter": (9.5, -14.5),
    "transport-belt": (5.5, -10.5),
    SCIENCE: (9.5, -10.5),
}
CHEST = (-3.5, -19.5)
INLET = (-0.5, -19.5)
OUTLET = (2.5, -19.5)
SPLITTER = (3.5, -19)
POLES = [
    (16.5, -15.5),  # Connects to M5's pole at (21.5, -18.5).
    (10.5, -12.5),  # Below the inherited water patch (x=10..13, y=-22..-17).
    (7.5, -12.5),
    (7.5, -16.5),
    (3.5, -17.5),
    (-2.5, -17.5),
]
# Keep the character west of the entire assembler block (x=4..11, y=-16..-9).
# The farthest assembler is sqrt(8**2 + 2**2) < 8.25 tiles away, leaving
# margin for move_to stopping short. Internal links/poles cannot seal this side.
WATCH = (1.5, -12.5)
CHEST_STATION = (-2.5, -22.5)
# North of the splitter's rotated 1x2 footprint (x=3..4, y=-20..-18),
# the belt rows and all feed inserters; open land continues north and west.
NORTH_STATION = (4.5, -22.5)
FUEL_STATION = (19.5, -16.5)
PLATE_STOCK = 64
BELT_PLATES = 12
CIRCUITS = 8
# Two packs consume two inserters and two belts (one belt craft). That needs
# three gears / six plates, three more plates and two circuits; the larger
# reserves also cover components buffered by the inserters and assemblers.

# (inserter kind/position, source kind/position, destination kind/position).
# Ordinary direction=0 picks north and drops south; horizontal links rotate.
LINKS = [
    (("inserter", (-2.5, -19.5)), ("iron-chest", CHEST), ("transport-belt", (-1.5, -19.5))),
    (
        ("long-handed-inserter", (5.5, -17.5)),
        ("transport-belt", (5.5, -19.5)),
        ("assembling-machine-1", ASSEMBLERS["iron-gear-wheel"]),
    ),
    (
        ("inserter", (9.5, -16.5)),
        ("transport-belt", (9.5, -17.5)),
        ("assembling-machine-1", ASSEMBLERS["inserter"]),
    ),
    *[
        (
            ("inserter", position),
            ("assembling-machine-1", ASSEMBLERS[source]),
            ("assembling-machine-1", ASSEMBLERS[destination]),
        )
        for position, source, destination in [
            ((7.5, -14.5), "iron-gear-wheel", "inserter"),
            ((5.5, -12.5), "iron-gear-wheel", "transport-belt"),
            ((9.5, -12.5), "inserter", SCIENCE),
            ((7.5, -10.5), "transport-belt", SCIENCE),
        ]
    ],
]


def action(operation, params):
    """Keep the complete failed movement status, including pathfinding detail."""
    if operation != "move_to":
        return shared_action(operation, params)
    started = call(operation, params)
    for _ in range(500):
        time.sleep(0.25)
        state = bridge.call("get_action_status", {"action_id": started["action_id"]})["result"]
        if state["status"] != "running":
            # The shared action helper truncates this JSON, hiding detail.
            print(operation + "_done", json.dumps(state, ensure_ascii=False), flush=True)
            if state["status"] != "succeeded":
                raise RuntimeError(f"{operation}: {state.get('error')}")
            return state
    print("move_to_timeout", json.dumps(state, ensure_ascii=False), flush=True)
    raise TimeoutError(operation)


def research_fixture():
    researched = call("get_research_status")["researched"] or []
    for technology in ("logistics", SCIENCE):
        if technology in researched:
            continue
        # Test-only research fixture, matching M14-U's logistics fixture. This
        # avoids another 75 red packs/lab wait and never grants inventory items.
        command = (
            f'/c game.forces.player.technologies["{technology}"].researched = true;'
            'rcon.print("m14w-research:"..tostring('
            f'game.forces.player.technologies["{technology}"].researched))'
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=5, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            result = client.run(command)
            if "m14w-research:true" not in result:
                result = client.run(command)
        assert "m14w-research:true" in result, (technology, result)
        print("M14-W test-only research fixture", technology, flush=True)
    researched = call("get_research_status")["researched"] or []
    assert all(name in researched for name in ("logistics", SCIENCE)), researched


def require_reach(observed, position, kind):
    """Use the actual arrival position, since move_to may stop short of its goal."""
    current = observed["position"]
    distance = math.dist((current["x"], current["y"]), position)
    assert distance <= observed["reach"][kind], (kind, position, distance, observed["reach"])


def put_batch(station, entries):
    """Preview, batch-place, then observe every actual placement from nearby land."""
    power.check_deadline(power.run_deadline)
    walk(station)
    observed = observe()
    params = []
    previews = []
    for name, position, direction, *kind in entries:
        require_reach(observed, position, "build")
        entry = {
            "item": name,
            "surface": "nauvis",
            "x": position[0],
            "y": position[1],
            "direction": direction,
        }
        if kind:
            entry["underground_type"] = kind[0]
        checked = call("check_placement", entry)
        assert checked["placeable"] and checked["has_item"], (entry, checked)
        params.append(entry)
        previews.append(checked)
    results = successful_batch_results(call("place_entities", {"items": params}), len(params))
    observed = observe()
    observed_again = observe()
    for entry, result, preview in zip(params, results, previews, strict=True):
        position = (entry["x"], entry["y"])
        entity = entity_at(observed, entry["item"], position)
        assert (
            result["target_ref"]
            == entity["target_ref"]
            == entity_at(observed_again, entry["item"], position)["target_ref"]
        ), (entry, result, entity)
        assert result["name"] == entity["name"], (entry, result, entity)
        if "pickup_position" in entity:
            assert_inserter_direction(entity, position, entry["direction"])
            for field in ("pickup_position", "drop_position"):
                for axis in ("x", "y"):
                    assert abs(preview[field][axis] - result[field][axis]) <= 0.01, (
                        entry,
                        preview,
                        result,
                    )
                    assert abs(result[field][axis] - entity[field][axis]) <= 0.01, (
                        entry,
                        result,
                        entity,
                    )
        # Assemblers without recipes, chests and poles may omit direction.
        if "direction" in entity:
            assert entity["direction"] == entry["direction"], (entry, entity)
        assert {axis: result["position"][axis] for axis in ("x", "y")} == {
            "x": position[0],
            "y": position[1],
        }, (entry, result)
    print("PASS batch placed", [(entry[0], entry[1]) for entry in entries], flush=True)


def orient_link(link, station):
    """Verify real pickup/drop footprints, including the long arm's 2.2 offset."""
    (name, position), (source_name, source_position), (dest_name, dest_position) = link
    walk(station)
    observed = observe()
    require_reach(observed, position, "interact")
    source = entity_at(observed, source_name, source_position)
    destination = entity_at(observed, dest_name, dest_position)
    if name == "inserter":
        orient_inserter(position, source["bounding_box"], destination["bounding_box"])
        return
    # The shared helper handles ordinary arms; verify the long arm's actual
    # two-tile pickup and 2.2-tile drop with the same footprint checks.
    for turns in range(4):
        inserter = entity_at(observe(), name, position)
        if position_in_tile_footprint(
            inserter["pickup_position"], source["bounding_box"]
        ) and position_in_tile_footprint(inserter["drop_position"], destination["bounding_box"]):
            print(
                "PASS link",
                name,
                position,
                inserter["pickup_position"],
                inserter["drop_position"],
                flush=True,
            )
            return
        if turns < 3:
            call("rotate_entity", {"target_ref": inserter["target_ref"]})
    raise AssertionError(f"Misaligned link {link}: {inserter}, source={source}, dest={destination}")


def feed_batch(station, entries):
    """Only raw plates/circuits may enter this line from the character."""
    power.check_deadline(power.run_deadline)
    walk(station)
    observed = observe()
    params = []
    for name, position, role, item, count in entries:
        assert item in {"iron-plate", "electronic-circuit"}, entries
        assert position != ASSEMBLERS[SCIENCE], "Never hand-feed green science ingredients"
        require_reach(observed, position, "interact")
        entity = entity_at(observed, name, position)
        params.append(
            {
                "target_ref": entity["target_ref"],
                "direction": "to_entity",
                "inventory_role": role,
                "item": item,
                "count": count,
            }
        )
    results = successful_batch_results(call("transfer_items", {"items": params}), len(params))
    for entry, result in zip(params, results, strict=True):
        assert result["moved"] == entry["count"], (entry, result)


def build():
    # Stay outside both the existing and future footprints while closing gaps.
    put_batch(WATCH, [("assembling-machine-1", position, 0) for position in ASSEMBLERS.values()])
    put_batch(WATCH, [(name, position, 0) for (name, position), _, _ in LINKS[3:]])
    walk(WATCH)
    for recipe, position in ASSEMBLERS.items():
        observed = observe()
        require_reach(observed, position, "interact")
        entity = entity_at(observed, "assembling-machine-1", position)
        call("set_recipe", {"target_ref": entity["target_ref"], "recipe": recipe})
        entity = entity_at(observe(), "assembling-machine-1", position)
        assert entity.get("recipe") == recipe, (recipe, entity)
        inventory = call("get_entity_inventory", {"target_ref": entity["target_ref"]})
        assert inventory["products_since_recipe"] == 0, (recipe, inventory)
        inventories = inventory["inventories"]
        assert not any(inventories.values()), (recipe, inventories)
    print("PASS all four fresh assembler inventories empty before feeding", flush=True)

    put_batch(
        CHEST_STATION,
        [
            ("iron-chest", CHEST, 0),
            ("inserter", (-2.5, -19.5), 0),
            ("transport-belt", (-1.5, -19.5), 4),
            ("underground-belt", INLET, 4, "input"),
        ],
    )
    put_batch(
        NORTH_STATION,
        [
            ("underground-belt", OUTLET, 4, "output"),
            ("splitter", SPLITTER, 4),
            ("transport-belt", (4.5, -19.5), 4),
            ("transport-belt", (5.5, -19.5), 4),
            *[("transport-belt", (x + 0.5, -18.5), 4) for x in range(4, 9)],
            ("transport-belt", (9.5, -18.5), 8),
        ],
    )
    put_batch(
        NORTH_STATION,
        [
            ("transport-belt", (9.5, -17.5), 8),
            ("long-handed-inserter", (5.5, -17.5), 0),
            ("inserter", (9.5, -16.5), 0),
        ],
    )
    observed = observe()
    for position, neighbour, kind in ((INLET, OUTLET, "input"), (OUTLET, INLET, "output")):
        belt = entity_at(observed, "underground-belt", position)
        assert belt["underground_type"] == kind and belt["neighbour"] == {
            "x": neighbour[0],
            "y": neighbour[1],
        }, belt
    print("PASS working plate path underground pair connected", flush=True)

    for index, link in enumerate(LINKS):
        orient_link(link, CHEST_STATION if index == 0 else NORTH_STATION if index < 3 else WATCH)
    # Small poles have 7.5-tile wire reach; supply coverage is checked against
    # the inherited lab's network for every assembler and inserter below.
    assert all(
        math.dist(a, b) <= 7.5 for a, b in zip([(21.5, -18.5), *POLES], POLES, strict=False)
    ), POLES
    for station, positions in [
        ((16, -13), POLES[:2]),
        (WATCH, POLES[2:3]),
        (NORTH_STATION, POLES[3:5]),
        ((0.5, -16.5), POLES[5:]),
    ]:
        put_batch(station, [("small-electric-pole", position, 0) for position in positions])


def check_wait_for_items():
    """M18-AL: wait for one more assembler-made pack without repeated reads."""
    target = entity_at(observe(), "assembling-machine-1", ASSEMBLERS[SCIENCE])
    params = {"target_ref": target["target_ref"]}
    inventories = call("get_entity_inventory", params)["inventories"]
    current = amount(inventories["result"], SCIENCE)
    if current >= 4:
        # A slow diagnostic pass can leave the result full. Collect existing
        # products through normal transfer, leaving room for another craft.
        collected = call(
            "transfer_item",
            {
                **params,
                "direction": "from_entity",
                "inventory_role": "result",
                "item": SCIENCE,
                "count": current,
            },
        )
        assert collected["moved"] == current, collected
        current = amount(call("get_entity_inventory", params)["inventories"]["result"], SCIENCE)
    waited = asyncio.run(
        create_server(bridge).call_tool(
            "wait_for_items",
            {
                **params,
                "item": SCIENCE,
                "count": current + 1,
                "inventory_role": "output",
                "timeout_seconds": 30,
            },
        )
    )
    result = json.loads(waited.content[0].text)
    assert not waited.is_error, result
    assert result["reached"] is True and result["current"] >= current + 1, result
    assert result["stalled"] is False and result["timed_out"] is False, result
    assert result["inventory_role"] == "result", result
    print("PASS M18-AL wait_for_items reached current+1", json.dumps(result), flush=True)


def check_wait_seconds():
    """M19-AQ: start and finish a short walk and cheap craft through MCP."""
    assert amount(items(), "iron-plate") >= 2, "Reserve two plates for the AQ craft"

    async def check():
        server = create_server(bridge)
        for operation, params in (
            # Open land just west of WATCH, outside the assembler block.
            ("move_to", {"surface": "nauvis", "x": WATCH[0] - 2, "y": WATCH[1]}),
            ("craft", {"recipe": "iron-gear-wheel", "count": 1}),
        ):
            waited = await server.call_tool(operation, {**params, "wait_seconds": 10})
            result = json.loads(waited.content[0].text)
            assert not waited.is_error, result
            assert result["status"] == "succeeded" and result["timed_out"] is False, result
            assert result["action_id"], result
            print("PASS M19-AQ", operation, json.dumps(result), flush=True)

    asyncio.run(check())


def check_wait_for_produced_items():
    """M21-AW: count new green packs from the machine's lifetime craft counter."""
    target = entity_at(observe(), "assembling-machine-1", ASSEMBLERS[SCIENCE])
    params = {"target_ref": target["target_ref"]}
    inventory = call("get_entity_inventory", params)
    assert SCIENCE in inventory["recipe_products"], inventory
    assert inventory["products_finished"] >= 1, inventory
    # Leave output space for another craft even if earlier diagnostics filled it.
    current = amount(inventory["inventories"]["result"], SCIENCE)
    if current:
        collected = call(
            "transfer_item",
            {
                **params,
                "direction": "from_entity",
                "inventory_role": "result",
                "item": SCIENCE,
                "count": current,
            },
        )
        assert collected["moved"] == current, collected
    waited = asyncio.run(
        create_server(bridge).call_tool(
            "wait_for_items",
            {**params, "item": SCIENCE, "count": 1, "produced": True, "timeout_seconds": 30},
        )
    )
    result = json.loads(waited.content[0].text)
    assert not waited.is_error, result
    assert result["reached"] is True and result["produced"] is True, result
    assert result["current"] >= 1 and not result["stalled"] and not result["timed_out"], result
    print("PASS M21-AW wait_for_items produced", json.dumps(result), flush=True)


def wait_for_science(held_components):
    deadline = min(time.monotonic() + 360, power.run_deadline)
    next_report = next_fuel = 0
    network_seen = False
    last = {}
    while time.monotonic() < deadline:
        if time.monotonic() >= next_fuel:
            walk(FUEL_STATION)
            boiler, _ = top_up("boiler", BOILER, threshold=8, target_count=10)
            fuel = bridge.call("get_entity_inventory", {"target_ref": boiler["target_ref"]})[
                "result"
            ]["inventories"]["fuel"]
            assert amount(fuel, "coal") >= 5, f"Boiler reserve too low: {fuel}; held={items()}"
            lab = entity_at(observe(), "lab", LAB)
            network = lab.get("electric_network_id")
            next_fuel = time.monotonic() + 30
        walk(WATCH)
        observed = observe()
        powered = [
            entity_at(observed, "assembling-machine-1", position)
            for position in ASSEMBLERS.values()
        ] + [entity_at(observed, name, position) for (name, position), _, _ in LINKS]
        assert network is not None and all(
            entity.get("electric_network_id") == network for entity in powered
        ), f"Power coverage differs from inherited lab: network={network}, entities={powered}"
        if not network_seen:
            print(
                "PASS all four assemblers and seven inserters share lab network",
                network,
                flush=True,
            )
            network_seen = True
        last = {}
        for recipe, position in ASSEMBLERS.items():
            entity = entity_at(observed, "assembling-machine-1", position)
            inventories = bridge.call("get_entity_inventory", {"target_ref": entity["target_ref"]})[
                "result"
            ]["inventories"]
            last[recipe] = {"status": entity.get("status"), "inventories": inventories}
        last["inserters"] = {
            f"{name}@{position}": entity_at(observed, name, position).get("status")
            for (name, position), _, _ in LINKS
        }
        last["plate_path"] = {
            f"{name}@{position}": entity_at(observed, name, position).get("belt_items", [])
            for name, position in [
                ("underground-belt", INLET),
                ("underground-belt", OUTLET),
                ("splitter", SPLITTER),
                ("transport-belt", (5.5, -19.5)),
                ("transport-belt", (9.5, -17.5)),
            ]
        }
        power.LAST_STATUSES.update(last)
        count = amount(last[SCIENCE]["inventories"]["result"], SCIENCE)
        if time.monotonic() >= next_report or count >= 2:
            print("green science line", json.dumps(last), flush=True)
            next_report = time.monotonic() + 15
        if count >= 2:
            # Fresh empty machines, only plates/circuits transferred, and verified
            # output links prove these consumed components were assembler-made.
            # Neither inserters nor belts (nor science) were transferred to green.
            current = items()
            assert {name: amount(current, name) for name in held_components} == held_components, (
                f"Character supplied line components: before={held_components}, after={current}"
            )
            print(
                "PASS green inputs came from inserter/belt assemblers", held_components, flush=True
            )
            check_wait_for_items()
            check_wait_for_produced_items()
            check_products_since_recipe()
            check_wait_seconds()
            print("GREEN_SCIENCE_LINE_SUCCEEDED", count, flush=True)
            return
        time.sleep(2)
    raise TimeoutError(
        f"Green assembler did not produce two packs within the remaining run/wait budget: {last}"
    )


def check_products_since_recipe():
    """M23-BD: the fresh recipe baseline counts production in single and batch reads."""
    target = entity_at(observe(), "assembling-machine-1", ASSEMBLERS[SCIENCE])
    params = {"target_ref": target["target_ref"]}
    inventory = call("get_entity_inventory", params)
    batch = call("get_entity_inventories", {"target_refs": [target["target_ref"]]})
    for result in (inventory, *successful_batch_results(batch, 1)):
        assert result["recipe"] == SCIENCE, result
        assert 1 <= result["products_since_recipe"] <= result["products_finished"], result
    print("PASS M23-BD products_since_recipe", json.dumps(inventory), flush=True)


def main():
    # power.walk (also imported by restore_pairs) resolves power.action at call
    # time, so gathering/service moves get the same untruncated diagnostics.
    power.action = action
    power.run_deadline = time.monotonic() + 870  # Leave CI's timeout room for diagnostics.
    research_fixture()
    restore_pairs()
    recipes = Recipes()
    equipment = {
        "assembling-machine-1": 4,
        "underground-belt": 2,
        "splitter": 1,
        "long-handed-inserter": 1,
        "small-electric-pole": len(POLES),
        "iron-chest": 1,
        "transport-belt": 10,
        "inserter": 6,
        "electronic-circuit": CIRCUITS,
    }
    stock = items()
    missing = {name: max(0, count - amount(stock, name)) for name, count in equipment.items()}
    needs = recipes.raw_needs(missing)
    needs["iron-plate"] += PLATE_STOCK + BELT_PLATES + 2  # Reserve one M19-AQ gear craft.
    needs["coal"] += 60  # Smelter servicing plus boiler reserve throughout the bounded wait.
    print("green science material needs", json.dumps(dict(needs)), flush=True)
    if amount(stock, "wood") < needs["wood"]:
        # Reuse M8-D's tree fixture, still standing after negative_checks.py.
        # Mining through the normal action yields wood; no new terrain or items.
        walk((0, -2))
        tree = entity_at(observe(), "tree-01", (0, -4))
        action("mine_entity", {"target_ref": tree["target_ref"], "count": 1})
        assert amount(items(), "wood") >= needs["wood"], (needs, items())
    gather(needs, min(time.monotonic() + 390, power.run_deadline))
    power.report("green science materials ready", force=True)
    for name, count in equipment.items():
        recipes.craft_to(name, count)
    walk(FUEL_STATION)
    top_up("boiler", BOILER, threshold=8, target_count=10)
    build()

    held_components = {name: amount(items(), name) for name in ("inserter", "transport-belt")}
    feed_batch(
        WATCH,
        [
            (
                "assembling-machine-1",
                ASSEMBLERS["transport-belt"],
                "source",
                "iron-plate",
                BELT_PLATES,
            ),
            (
                "assembling-machine-1",
                ASSEMBLERS["inserter"],
                "source",
                "electronic-circuit",
                CIRCUITS,
            ),
        ],
    )
    feed_batch(CHEST_STATION, [("iron-chest", CHEST, "main", "iron-plate", PLATE_STOCK)])
    print("green science line fed: plates -> underground -> splitter -> gear/inserter", flush=True)
    wait_for_science(held_components)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, RuntimeError, TimeoutError, BridgeError):
        print(
            "green science failed",
            json.dumps(
                {
                    "inventory": items(),
                    "status": power.LAST_STATUSES,
                }
            ),
            flush=True,
        )
        raise
