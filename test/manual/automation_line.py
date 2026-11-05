"""Automated red science on the disposable RCON 27277 save, after power_research.py.

Uses only normal tools for items and equipment. Science packs are never hand crafted or
transferred: the assembler and inserters must supply every pack to the M4 lab.
The M14-U smoke checks may unlock logistics with a research-only test fixture.
"""

import json
import math
import time

import power_research as power
from helpers import (
    position_in_tile_footprint,
    select_automation_technology,
    successful_batch_results,
)
from power_research import (
    BOILER,
    COPPER_DRILL,
    IRON_DRILLS,
    LAB,
    STATION,
    Recipes,
    amount,
    bridge,
    call,
    entity_at,
    gather,
    items,
    observe,
    place,
    tile_center,
    top_up,
    transfer,
    walk,
)
from rcon.source import Client

from factorio_mcp.bridge import RCON_FRAG_THRESHOLD, BridgeError
from production_line import action, password

ASSEMBLER = (23.5, -19.5)
OUTPUT_INSERTER = (23.5, -21.5)
INPUT_INSERTER = (23.5, -17.5)
CHEST = (23.5, -16.5)
POLE = (21.5, -18.5)
SCIENCE = "automation-science-pack"
PLACEABLE_EQUIPMENT = {
    "underground-belt": 2,
    "splitter": 1,
    "long-handed-inserter": 1,
    "pipe-to-ground": 2,
}


def restore_pairs():
    """Reconstruct M4's in-memory pair list from the equipment still on the save."""
    walk(STATION)
    observed = observe()
    power.PAIRS.clear()
    for plate, position in [
        *(("iron-plate", position) for position in IRON_DRILLS),
        ("copper-plate", COPPER_DRILL),
    ]:
        drill = entity_at(observed, "burner-mining-drill", position)
        drop = drill["drop_position"]
        centered = tile_center(drop)
        furnace_position = (centered[0] + 0.5, centered[1] - 0.5)
        furnace = entity_at(observed, "stone-furnace", furnace_position)
        assert position_in_tile_footprint(drop, furnace["bounding_box"]), (
            f"Inherited pair misaligned: drill={position}, drop={drop}, furnace={furnace_position}"
        )
        power.PAIRS.append((plate, position, furnace_position))
    print("inherited drill/furnace pairs", power.PAIRS, flush=True)


def orient_inserter(position, source_box, destination_box):
    """Observe pickup/drop tiles and correct the direction with at most 3 turns."""
    for turns in range(4):
        inserter = entity_at(observe(), "inserter", position)
        pickup = inserter["pickup_position"]
        drop = inserter["drop_position"]
        if position_in_tile_footprint(pickup, source_box) and position_in_tile_footprint(
            drop, destination_box
        ):
            print(f"inserter at {position}: pickup={pickup}, drop={drop} verified", flush=True)
            return
        if turns < 3:
            call("rotate_entity", {"target_ref": inserter["target_ref"]})
    raise AssertionError(
        f"Inserter at {position} misaligned after 3 rotations: pickup={pickup}, drop={drop}, "
        f"source footprint={source_box}, destination footprint={destination_box}"
    )


def planned_box(name, position):
    # The design places each inserter before its source. Use the source's
    # placement footprint now, then verify again against the actual entity.
    checked = call(
        "check_placement",
        {"item": name, "surface": "nauvis", "x": position[0], "y": position[1], "direction": 0},
    )
    assert checked["placeable"] and checked["has_item"], (name, position, checked)
    return checked["bounding_box"]


def check_new_placeables(recipes):
    research = call("get_research_status")
    if "logistics" not in (research["researched"] or []):
        # Test-only research fixture, like the existing resource/tile fixtures.
        # Another 20 packs/300 lab seconds would test research again; no items
        # are granted. All equipment below is crafted from gathered materials.
        fixture = (
            '/c game.forces.player.technologies["logistics"].researched = true;'
            'rcon.print("m14u-logistics:"..tostring('
            'game.forces.player.technologies["logistics"].researched))'
        )
        with Client(
            "127.0.0.1", 27277, passwd=password, timeout=5, frag_threshold=RCON_FRAG_THRESHOLD
        ) as client:
            result = client.run(fixture)
            if "m14u-logistics:true" not in result:
                result = client.run(fixture)
        assert "m14u-logistics:true" in result, result
        print("M14-U test-only logistics research fixture used", flush=True)
    assert "logistics" in (call("get_research_status")["researched"] or [])
    for name, count in PLACEABLE_EQUIPMENT.items():
        info = recipes.info(name, refresh=True)
        assert info["enabled"] and info["hand_craftable"], info
        print("M14-U recipe", name, json.dumps(info), flush=True)
        recipes.craft_to(name, count)

    before = {name: amount(items(), name) for name in PLACEABLE_EQUIPMENT}
    placed = []

    def put(name, position, direction=0, underground_type=None):
        walk((position[0] - 1.5, position[1] + 1.5))
        observe()
        params = {
            "item": name,
            "surface": "nauvis",
            "x": position[0],
            "y": position[1],
            "direction": direction,
        }
        if underground_type is not None:
            params["underground_type"] = underground_type
        stock = amount(items(), name)
        checked = call("check_placement", params)
        assert checked["placeable"] and checked["has_item"], checked
        assert amount(items(), name) == stock, checked
        result = call("place_entity", params)
        placed.append((name, position))
        assert checked["position"] == {"x": result["position"]["x"], "y": result["position"]["y"]}
        assert amount(items(), name) == stock - 1, result
        entity = entity_at(observe(), name, position)
        assert checked["bounding_box"] == entity["bounding_box"], (checked, entity)
        assert not entity.get("inventory_roles"), entity
        return entity

    try:
        # Base's bundled Factoriopedia blueprint uses the same direction for
        # input and output; output direction is where items leave, here east.
        inlet, outlet = (25.5, -10.5), (28.5, -10.5)
        unpaired = put("underground-belt", inlet, 4)  # Default type is input.
        assert unpaired["underground_type"] == "input" and "neighbour" not in unpaired, unpaired
        put("underground-belt", outlet, 4, "output")
        observed = observe()
        for position, paired, underground_type in (
            (inlet, outlet, "input"),
            (outlet, inlet, "output"),
        ):
            entity = entity_at(observed, "underground-belt", position)
            assert entity["direction"] == 4 and entity["underground_type"] == underground_type, (
                entity
            )
            assert entity["neighbour"] == {"x": paired[0], "y": paired[1]}, entity
            assert not entity.get("belt_items", []), entity
        # Exercise the Mod's validation directly as well as MCP's unit tests.
        for operation in ("check_placement", "place_entity"):
            for item, underground_type in (
                ("transport-belt", "input"),
                ("transport-belt", "invalid"),
                ("underground-belt", "invalid"),
            ):
                try:
                    bridge.call(
                        operation,
                        {
                            "item": item,
                            "surface": "nauvis",
                            "x": 27.5,
                            "y": -9.5,
                            "underground_type": underground_type,
                        },
                    )
                except BridgeError as error:
                    assert error.code == "invalid_input", error
                else:
                    raise AssertionError(f"{operation} accepted {item}/{underground_type}")
        print("underground belt pair connects", flush=True)

        splitter = put("splitter", (27, -7.5))
        assert splitter["type"] == "splitter", splitter
        assert not splitter.get("belt_items", []), splitter
        position = (26.5, -4.5)
        inserter = put("long-handed-inserter", position)
        assert inserter["type"] == "inserter", inserter
        assert inserter["pickup_position"] == {"x": position[0], "y": position[1] - 2}, inserter
        # Base 2.0.77 insert_position is (0, 2.2): the addressed tile is two
        # tiles away, even though its drop coordinate is offset by 2.2.
        assert tile_center(inserter["drop_position"]) == (position[0], position[1] + 2), inserter
        assert math.isclose(inserter["drop_position"]["y"] - position[1], 2.2, abs_tol=0.01), (
            inserter
        )
        print("splitter and long-handed inserter placed", flush=True)

        pipes = ((25.5, -1.5), (28.5, -1.5))
        # The base prototype's underground port faces south when direction=0;
        # rotate it west/east so the underground ports point toward each other.
        put("pipe-to-ground", pipes[0], 12)
        put("pipe-to-ground", pipes[1], 4)
        observed = observe()
        for position in pipes:
            pipe = entity_at(observed, "pipe-to-ground", position)
            connections = pipe.get("fluid_connections", [])
            assert any(
                c["connection_type"] == "underground" and c["connected"] is True
                for c in connections
            ), pipe
            assert any(c["connection_type"] == "normal" for c in connections), pipe
        print("pipe-to-ground pair connects", flush=True)
    finally:
        for name, position in reversed(placed):
            walk((position[0] - 1.5, position[1] + 1.5))
            target = entity_at(observe(), name, position)
            stock = amount(items(), name)
            action("mine_entity", {"target_ref": target["target_ref"], "count": 1})
            assert amount(items(), name) == stock + 1, (name, items())
            assert not any(
                e["name"] == name and (e["x"], e["y"]) == position for e in observe()["entities"]
            ), (name, position)
        assert {name: amount(items(), name) for name in before} == before, items()
        walk((20, -18))
    print("M14-U placed equipment recovered to inventory", flush=True)


def check_machine_inventories():
    """M17-AI: inventory reads include the machine state and configured recipe."""
    walk((20, -18))
    observation = bridge.call(
        "observe_nearby",
        {
            "names": ["assembling-machine-1", "lab", "iron-chest"],
            "include_resources": False,
            "include_tiles": False,
        },
    )
    targets = [
        entity_at(observation["result"], name, position)
        for name, position in (
            ("assembling-machine-1", ASSEMBLER),
            ("lab", LAB),
            ("iron-chest", CHEST),
        )
    ]
    single = [
        bridge.call("get_entity_inventory", {"target_ref": target["target_ref"]})
        for target in targets
    ]
    batch = bridge.call(
        "get_entity_inventories", {"target_refs": [target["target_ref"] for target in targets]}
    )
    batch_results = successful_batch_results(batch["result"], len(targets))
    for index, target in enumerate(targets):
        for tick, result in (
            (single[index]["tick"], single[index]["result"]),
            (batch["tick"], batch_results[index]),
        ):
            status = result.get("status")
            if target.get("status") is None:
                assert "status" not in result, (target, result)
            else:
                assert isinstance(status, str) and status, (target, result)
                if tick == observation["tick"]:
                    assert status == target["status"], (target, result)
            if index == 0:
                assert isinstance(status, str) and status, result
                assert result.get("recipe") == SCIENCE, result
            else:
                assert "recipe" not in result, result
    print("M17-AI single and batch inventories include status and assembler recipe", flush=True)


def main():
    power.run_deadline = time.monotonic() + 885  # Leave room before CI's 900 s timeout.
    research = call("get_research_status")
    technology = select_automation_technology(research["available"] or [])
    packs = technology["unit_count"] * technology["ingredients"][0]["amount"]
    print("chosen technology", json.dumps(technology), "required packs", packs, flush=True)

    recipes = Recipes()
    equipment = {
        "assembling-machine-1": 1,
        "inserter": 2,
        "iron-chest": 1,
        "small-electric-pole": 2,  # One installed, one spare (the recipe yields two).
    }
    needs = recipes.raw_needs(
        {**equipment, **PLACEABLE_EQUIPMENT, "copper-plate": packs, "iron-gear-wheel": packs}
    )
    # Reserve fuel for the lab, assembler, and two inserters throughout polling;
    # gather also mines more coal as servicing the inherited smelters consumes it.
    needs["coal"] += 40
    print("computed needs", json.dumps(dict(needs)), flush=True)
    restore_pairs()
    try:
        # Include smelting time for the additional M14-U equipment reserves.
        gather(needs, time.monotonic() + 300)
    except TimeoutError as error:
        raise TimeoutError(
            f"M5 material gathering timed out; needs={dict(needs)}: {error}"
        ) from error
    for name, count in equipment.items():
        recipes.craft_to(name, count)

    walk((20, -18))
    lab = entity_at(observe(), "lab", LAB)
    lab_input = bridge.call("get_entity_inventory", {"target_ref": lab["target_ref"]})["result"][
        "inventories"
    ]["input"]
    character_packs = amount(items(), SCIENCE)
    lab_packs = amount(lab_input, SCIENCE)
    assert character_packs == lab_packs == 0, (
        f"Leftover hand-crafted science packs from power_research: character={character_packs}, "
        f"lab input={lab_packs}; both must be empty before the automated line starts"
    )

    assembler_box = planned_box("assembling-machine-1", ASSEMBLER)
    place("inserter", OUTPUT_INSERTER)
    orient_inserter(OUTPUT_INSERTER, assembler_box, lab["bounding_box"])
    assembler = place("assembling-machine-1", ASSEMBLER)
    chest_box = planned_box("iron-chest", CHEST)
    place("inserter", INPUT_INSERTER)
    orient_inserter(INPUT_INSERTER, chest_box, assembler["bounding_box"])
    place("iron-chest", CHEST)
    place("small-electric-pole", POLE)

    # Recheck both links using observed boxes once every endpoint exists.
    observed = observe()
    assembler = entity_at(observed, "assembling-machine-1", ASSEMBLER)
    lab = entity_at(observed, "lab", LAB)
    chest = entity_at(observed, "iron-chest", CHEST)
    for position, source, destination in (
        (OUTPUT_INSERTER, assembler, lab),
        (INPUT_INSERTER, chest, assembler),
    ):
        inserter = entity_at(observed, "inserter", position)
        assert position_in_tile_footprint(
            inserter["pickup_position"], source["bounding_box"]
        ) and position_in_tile_footprint(inserter["drop_position"], destination["bounding_box"]), (
            f"Observed endpoint footprint mismatch at {position}: inserter={inserter}, "
            f"source={source}, destination={destination}"
        )
    assembler = entity_at(observe(), "assembling-machine-1", ASSEMBLER)
    call("set_recipe", {"target_ref": assembler["target_ref"], "recipe": SCIENCE})
    recipes.craft_to("iron-gear-wheel", packs)
    for item in ("copper-plate", "iron-gear-wheel"):
        remaining = packs
        while remaining:
            power.check_deadline(power.run_deadline)
            chest = entity_at(observe(), "iron-chest", CHEST)
            count = min(remaining, 100)
            transfer(chest, "main", item, count)
            remaining -= count

    call("start_research", {"technology": technology["name"]})
    # No science-pack transfers anywhere in this script, including after this point.
    deadline = min(time.monotonic() + 600, power.run_deadline)
    next_report = 0
    network_seen = False
    working_seen = False
    last = {}
    while time.monotonic() < deadline:
        boiler, _ = top_up("boiler", BOILER, threshold=6, target_count=10)
        # top_up returns the pre-transfer inventory; fetch again to assert the reserve.
        fuel = bridge.call("get_entity_inventory", {"target_ref": boiler["target_ref"]})["result"][
            "inventories"
        ]["fuel"]
        assert amount(fuel, "coal") >= 5, f"Boiler coal below 5: {fuel}; inventory={items()}"
        observed = observe()
        entities = {
            "assembler": entity_at(observed, "assembling-machine-1", ASSEMBLER),
            "output_inserter": entity_at(observed, "inserter", OUTPUT_INSERTER),
            "input_inserter": entity_at(observed, "inserter", INPUT_INSERTER),
            "lab": entity_at(observed, "lab", LAB),
        }
        lab_input = bridge.call(
            "get_entity_inventory", {"target_ref": entities["lab"]["target_ref"]}
        )["result"]["inventories"]["input"]
        research = bridge.call("get_research_status")["result"]
        last = {
            name: {key: entity.get(key) for key in ("status", "electric_network_id", "recipe")}
            for name, entity in entities.items()
        }
        last.update(
            lab_input=lab_input,
            current=research.get("current"),
            progress=research.get("progress"),
            boiler_coal=amount(fuel, "coal"),
        )
        power.LAST_STATUSES.update(last)
        network = entities["lab"].get("electric_network_id")
        if not network_seen and all(
            entity.get("electric_network_id") is not None for entity in entities.values()
        ):
            assert all(entity["electric_network_id"] == network for entity in entities.values()), (
                f"Assembler, inserters, and lab have different electric networks: {last}"
            )
            network_seen = True
            print(
                "assembler, both inserters, and lab share electric_network_id", network, flush=True
            )
        if not working_seen and entities["assembler"].get("status") == "working":
            working_seen = True
            print("assembler status=working observed", flush=True)
        if time.monotonic() >= next_report:
            print("automation line", json.dumps(last), flush=True)
            next_report = time.monotonic() + 15
        if technology["name"] in (research["researched"] or []):
            assert network_seen, f"Shared electric network never observed: {last}"
            assert working_seen, f"Assembler working status never observed: {last}"
            check_new_placeables(recipes)
            check_machine_inventories()
            print("AUTOMATION_LINE_SUCCEEDED", technology["name"], flush=True)
            return
        time.sleep(2)
    raise TimeoutError(
        f"M5 research timed out: technology={technology['name']}, "
        f"network_seen={network_seen}, working_seen={working_seen}, last statuses={last}"
    )


if __name__ == "__main__":
    main()
