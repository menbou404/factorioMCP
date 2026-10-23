"""Manual logistics test for a disposable save on RCON port 27277 only.

Run after production_line.py and negative_checks.py on the same server.
Creates only raw-resource fixtures with /c; all items come from normal actions.
"""

import json
import math
import time

from helpers import position_in_tile_footprint
from rcon.source import Client

from factorio_mcp.bridge import RCON_FRAG_THRESHOLD
from production_line import action, amount, bridge, call, password, reference


def observe():
    observed = bridge.call("observe_nearby")["result"]
    entities = list(observed["entities"] or [])
    offset = observed.get("next_entity_offset")
    while offset is not None:
        page = bridge.call("observe_nearby", {"entity_offset": offset})["result"]
        entities.extend(page["entities"] or [])
        offset = page.get("next_entity_offset")
    observed["entities"] = entities
    return observed


def entity_at(observed, name, position):
    x, y = position
    for entity in observed["entities"]:
        if entity["name"] == name and entity["x"] == x and entity["y"] == y:
            return entity
    raise RuntimeError(f"{name} at {position} not observed")


def tile_center(position):
    return math.floor(position["x"]) + 0.5, math.floor(position["y"]) + 0.5


def place(item, position, direction=0):
    step = "observe"
    try:
        observe()
        x, y = position
        params = {"item": item, "surface": "nauvis", "x": x, "y": y, "direction": direction}
        step = "check_placement"
        checked = call("check_placement", params)
        assert checked["placeable"], f"Cannot place {item} at {position}: {checked}"
        assert checked["has_item"], f"Missing {item} at {position}: {checked}"
        step = "place_entity"
        call("place_entity", params)
        step = "verify_placement"
        return entity_at(observe(), item, position)
    except Exception as error:
        print(
            f"placement failed: step={step}, item={item}, position={position}, "
            f"direction={direction}: {error}",
            flush=True,
        )
        raise


def main():
    action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
    before = bridge.call("get_inventory")["result"]["items"]
    for name, position in [("stone-furnace", (6, 4)), ("burner-mining-drill", (6, 6))]:
        target = entity_at(observe(), name, position)
        action("mine_entity", {"target_ref": target["target_ref"], "count": 1})
        remaining = observe()["entities"]
        assert not any(
            entity["name"] == name and (entity["x"], entity["y"]) == position
            for entity in remaining
        ), f"{name} was not picked up"
        after = bridge.call("get_inventory")["result"]["items"]
        assert amount(after, name) == amount(before, name) + 1, f"{name} item not recovered"
    print("pickup ok", flush=True)

    fixture = (
        "/c local s=game.surfaces.nauvis;local n=0;"
        "for _,p in ipairs({{-12.5,10.5},{-11.5,10.5},{-12.5,11.5},{-11.5,11.5}}) do "
        'local e=s.find_entity("iron-ore",p);if not e then '
        'e=s.create_entity{name="iron-ore",position=p,amount=300} end;'
        'if e then n=n+1 end end;rcon.print("fixture:"..n)'
    )
    with Client(
        "127.0.0.1", 27277, passwd=password, timeout=5, frag_threshold=RCON_FRAG_THRESHOLD
    ) as client:
        result = client.run(fixture)
        if "fixture:" not in result:
            result = client.run(fixture)
        print(result.strip(), flush=True)
        assert "fixture:4" in result, f"Iron-ore fixture failed: {result}"

    action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
    action("mine_entity", {"target_ref": reference("coal"), "count": 6})
    items = bridge.call("get_inventory")["result"]["items"]
    gears_needed = max(0, 2 - amount(items, "iron-gear-wheel"))
    plates_needed = 2 * gears_needed + 2  # One plate each for the belt and inserter crafts.
    plates = amount(items, "iron-plate")
    if plates < plates_needed:
        raise RuntimeError(
            f"Insufficient iron plates from pickup: need {plates_needed}, have {plates}"
        )
    if gears_needed:
        action("craft", {"recipe": "iron-gear-wheel", "count": gears_needed})
    action("craft", {"recipe": "transport-belt", "count": 1})
    action("craft", {"recipe": "burner-inserter", "count": 1})

    action("move_to", {"surface": "nauvis", "x": -9, "y": 13})
    drill_position = (-12, 11)
    drill = place("burner-mining-drill", drill_position, 4)
    belt1_position = tile_center(drill["drop_position"])
    belt2_position = (belt1_position[0] + 1, belt1_position[1])
    place("transport-belt", belt1_position, 4)
    place("transport-belt", belt2_position, 4)
    inserter_position = (belt2_position[0] + 1, belt2_position[1])
    inserter = place("burner-inserter", inserter_position)
    for _ in range(3):
        if tile_center(inserter["pickup_position"]) == belt2_position:
            break
        call("rotate_entity", {"target_ref": inserter["target_ref"]})
        inserter = entity_at(observe(), "burner-inserter", inserter_position)
    assert tile_center(inserter["pickup_position"]) == belt2_position, inserter
    drop = inserter["drop_position"]
    assert tile_center(drop) == (inserter_position[0] + 1, inserter_position[1]), inserter
    furnace_position = (math.floor(drop["x"]) + 1, math.floor(drop["y"]) + 1)
    furnace = place("stone-furnace", furnace_position)
    assert position_in_tile_footprint(drop, furnace["bounding_box"]), (
        f"logistics verify_drop_tile failed: inserter={inserter_position}, "
        f"drop={drop}, furnace={furnace_position}, bounding_box={furnace['bounding_box']}"
    )

    for name, position, count in [
        ("burner-mining-drill", drill_position, 2),
        ("burner-inserter", inserter_position, 1),
        ("stone-furnace", furnace_position, 2),
    ]:
        target = entity_at(observe(), name, position)
        moved = call(
            "transfer_item",
            {
                "target_ref": target["target_ref"],
                "direction": "to_entity",
                "inventory_role": "fuel",
                "item": "coal",
                "count": count,
            },
        )
        assert moved["moved"] == count, moved

    deadline = time.monotonic() + 120
    next_report = time.monotonic()
    last_statuses = {}
    plates = 0
    while time.monotonic() < deadline:
        observed = observe()
        drill = entity_at(observed, "burner-mining-drill", drill_position)
        inserter = entity_at(observed, "burner-inserter", inserter_position)
        furnace = entity_at(observed, "stone-furnace", furnace_position)
        last_statuses = {
            "drill": drill.get("status"),
            "inserter": inserter.get("status"),
            "furnace": furnace.get("status"),
        }
        result = bridge.call("get_entity_inventory", {"target_ref": furnace["target_ref"]})[
            "result"
        ]
        plates = amount(result["inventories"]["result"], "iron-plate")
        now = time.monotonic()
        if now >= next_report:
            iron = next(
                (entry for entry in observed["resources"] or [] if entry["name"] == "iron-ore"),
                None,
            )
            belts = [
                entity_at(observed, "transport-belt", position).get("belt_items", [])
                for position in (belt1_position, belt2_position)
            ]
            print(
                "logistics",
                json.dumps(
                    {"iron-ore": iron, "belt_items": belts, "status": last_statuses},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            next_report = now + 5
        if plates >= 5:
            print("LOGISTICS_LINE_SUCCEEDED", plates, flush=True)
            return
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise RuntimeError(f"Only {plates} automated plates; last statuses: {last_statuses}")


if __name__ == "__main__":
    main()
