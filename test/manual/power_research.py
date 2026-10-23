"""Power and lab research on the disposable RCON 27277 save, after logistics_line.py.

Only resource entities and water tiles are created by /c. All equipment and
science packs come from pickup, mining, smelting, and normal hand crafting.
"""

import json
import math
import time
from collections import Counter

from helpers import position_in_tile_footprint
from logistics_line import entity_at, observe, place, tile_center
from rcon.source import Client

from factorio_mcp.bridge import RCON_FRAG_THRESHOLD, BridgeError
from production_line import action, amount, bridge, call, password

RAW = {"iron-plate", "copper-plate", "stone", "coal", "wood"}
STATION = (-16, -8)
IRON_DRILLS = [(-21, -10), (-19, -10)]
COPPER_DRILL = (-11, -10)
RESOURCE_POSITIONS = {"stone": (-5.5, -10.5), "coal": (-5.5, -14.5)}
PUMP = (14.5, -19.5)
BOILER = (16.5, -20)
ENGINE = (16.5, -23.5)
LAB = (23.5, -23.5)
POLES = [(18.5, -23.5), (21.5, -23.5)]
PAIRS = []
LAST_STATUSES = {}
next_report = 0
run_deadline = 0


def items():
    return bridge.call("get_inventory")["result"]["items"]


def walk(position):
    current = bridge.call("get_agent_status")["result"]["position"]
    if math.dist((current["x"], current["y"]), position) <= 0.5:
        return
    action("move_to", {"surface": "nauvis", "x": position[0], "y": position[1]})


def report(label, force=False):
    global next_report
    if force or time.monotonic() >= next_report:
        current = items()
        snapshot = {
            name: amount(current, name) for name in sorted(RAW | {"automation-science-pack"})
        }
        print(label, json.dumps({"inventory": snapshot, "status": LAST_STATUSES}), flush=True)
        next_report = time.monotonic() + 15


def check_deadline(deadline):
    if time.monotonic() >= min(deadline, run_deadline):
        raise TimeoutError(f"Gather/run deadline: inventory={items()}, statuses={LAST_STATUSES}")


def transfer(target, role, item, count, direction="to_entity"):
    moved = call(
        "transfer_item",
        {
            "target_ref": target["target_ref"],
            "direction": direction,
            "inventory_role": role,
            "item": item,
            "count": count,
        },
    )
    assert moved["moved"] == count, moved


def pickup():
    walk((-9, 14))
    names = {"burner-mining-drill", "stone-furnace", "transport-belt", "burner-inserter"}
    targets = [
        (e["name"], (e["x"], e["y"]))
        for e in observe()["entities"]
        if e["owned"] and e["name"] in names
    ]
    assert Counter(name for name, _ in targets) == Counter(
        {"burner-mining-drill": 1, "stone-furnace": 1, "transport-belt": 2, "burner-inserter": 1}
    ), targets
    for name, position in targets:
        before = amount(items(), name)
        target = entity_at(observe(), name, position)
        action("mine_entity", {"target_ref": target["target_ref"], "count": 1})
        assert amount(items(), name) == before + 1, (name, items())
        assert not any(
            e["name"] == name and (e["x"], e["y"]) == position for e in observe()["entities"]
        ), (name, position)
    print("pickup ok", flush=True)


def fixture():
    command = (
        "/c local s=game.surfaces.nauvis;local counts={};"
        'for _,v in ipairs({{"iron-ore",-22,-11,4},{"copper-ore",-12,-11,4},'
        '{"stone",-6,-11,2},{"coal",-6,-15,2}}) do local n=0;'
        "for x=v[2],v[2]+v[4]-1 do for y=v[3],v[3]+v[4]-1 do "
        "local p={x+0.5,y+0.5};local e=s.find_entity(v[1],p);"
        "if not e then e=s.create_entity{name=v[1],position=p,amount=2000} end;"
        "if e and e.amount==2000 then n=n+1 end end end;counts[v[1]]=n end;"
        "local t={};for x=10,13 do for y=-22,-17 do "
        't[#t+1]={name="water",position={x,y}} end end;s.set_tiles(t);'
        'local water=0;for _,v in ipairs(t) do if s.get_tile(v.position).name=="water" '
        'then water=water+1 end end;counts.water=water;rcon.print("fixture:"..'
        "helpers.table_to_json(counts))"
    )
    with Client(
        "127.0.0.1", 27277, passwd=password, timeout=5, frag_threshold=RCON_FRAG_THRESHOLD
    ) as client:
        result = client.run(command)
        if "fixture:" not in result:
            result = client.run(command)
    print(result.strip(), flush=True)
    assert "fixture:" in result, result
    counts = json.JSONDecoder().raw_decode(result.split("fixture:", 1)[1])[0]
    assert counts == {"iron-ore": 16, "copper-ore": 16, "stone": 4, "coal": 4, "water": 24}, counts


class Recipes:
    def __init__(self):
        self.cache = {}

    def info(self, name, refresh=False):
        if refresh or name not in self.cache:
            self.cache[name] = bridge.call("get_recipe_info", {"recipe": name})["result"]
        return self.cache[name]

    def output(self, name):
        products = self.info(name)["products"]
        product = next(p for p in products if p["name"] == name)
        assert product.get("probability", 1) == 1 and "amount" in product, product
        return product["amount"]

    def raw_needs(self, targets):
        needs = Counter()
        surplus = Counter()

        def expand(name, count, path=()):
            used = min(surplus[name], count)
            surplus[name] -= used
            count -= used
            if not count:
                return
            if name in RAW:
                needs[name] += count
                return
            assert name not in path, f"Recipe cycle: {path}, {name}"
            info = self.info(name)
            crafts = math.ceil(count / self.output(name))
            surplus[name] += crafts * self.output(name) - count
            for ingredient in info["ingredients"]:
                assert ingredient["type"] == "item", ingredient
                expand(ingredient["name"], crafts * ingredient["amount"], (*path, name))

        for name, count in targets.items():
            expand(name, count)
        return needs

    def craft_to(self, name, target_count):
        check_deadline(run_deadline)
        stock = Counter({item["name"]: item["count"] for item in items()})
        plan = []

        def reserve(item, count, path=()):
            used = min(stock[item], count)
            stock[item] -= used
            missing = count - used
            if missing <= 0:
                return
            if item in RAW:
                raise RuntimeError(
                    f"Missing raw material {item}: short {missing}, inventory={items()}"
                )
            assert item not in path, f"Recipe cycle: {path}, {item}"
            info = self.info(item, refresh=True)
            assert info["enabled"] and info["hand_craftable"], f"Recipe unavailable: {info}"
            crafts = math.ceil(missing / self.output(item))
            for ingredient in info["ingredients"]:
                reserve(ingredient["name"], crafts * ingredient["amount"], (*path, item))
            stock[item] += crafts * self.output(item) - missing
            plan.append((item, crafts, info["energy"]))

        # Reserve sibling ingredients as well: e.g. the lab's belts need gears
        # in addition to the gears allocated directly to the lab itself.
        reserve(name, target_count)
        for recipe, crafts, energy in plan:
            while crafts:
                # Short batches also keep inventory progress visible during crafting.
                batch = min(crafts, 100, max(1, int(12 / energy)))
                action("craft", {"recipe": recipe, "count": batch})
                crafts -= batch
                report("crafting")
                check_deadline(run_deadline)
        assert amount(items(), name) >= target_count, (name, target_count, items())


def top_up(name, position, threshold=2, target_count=6):
    target = entity_at(observe(), name, position)
    contents = bridge.call("get_entity_inventory", {"target_ref": target["target_ref"]})["result"][
        "inventories"
    ]
    fuel = amount(contents["fuel"], "coal")
    if fuel < threshold:
        count = min(target_count - fuel, amount(items(), "coal"))
        if count:
            transfer(target, "fuel", "coal", count)
    LAST_STATUSES[f"{name}@{position}"] = {"status": target.get("status"), "coal": fuel}
    return target, contents


def service():
    if not PAIRS:
        return
    walk(STATION)
    for plate, drill_position, furnace_position in PAIRS:
        top_up("burner-mining-drill", drill_position)
        furnace, contents = top_up("stone-furnace", furnace_position)
        count = amount(contents["result"], plate)
        if count:
            transfer(furnace, "result", plate, min(count, 100), "from_entity")
    report("smelting")


def mine_raw(name, count):
    position = RESOURCE_POSITIONS[name]
    walk((position[0] + 2, position[1]))
    target = entity_at(observe(), name, position)
    action("mine_entity", {"target_ref": target["target_ref"], "count": min(count, 10)})
    report("mining")


def gather(needs, deadline):
    while True:
        check_deadline(deadline)
        service()
        current = items()
        missing = {
            name: count - amount(current, name)
            for name, count in needs.items()
            if amount(current, name) < count
        }
        if not missing:
            return
        if "wood" in missing:
            raise RuntimeError(
                f"Missing inherited wood for poles: needs={needs}, inventory={current}"
            )
        # Reserve coal while smelting continues, even when the recipe plan needs none.
        if PAIRS and amount(current, "coal") < 12:
            mine_raw("coal", 10)
        elif "coal" in missing:
            mine_raw("coal", missing["coal"])
        elif "stone" in missing:
            mine_raw("stone", missing["stone"])
        else:
            if not PAIRS:
                raise RuntimeError(f"Cannot bootstrap plates: needs={needs}, inventory={current}")
            time.sleep(2)


def build_pair(plate, position):
    step = "walk_to_station"
    furnace_position = None
    drop = None
    try:
        walk(STATION)
        step = "place_drill"
        drill = place("burner-mining-drill", position)
        step = "locate_furnace"
        drop = drill["drop_position"]
        # North-facing drill: round the drop toward the north to keep the two
        # collision boxes separate (production_line also spaces them two tiles apart).
        centered = tile_center(drop)
        furnace_position = (centered[0] + 0.5, centered[1] - 0.5)
        step = "place_furnace"
        furnace = place("stone-furnace", furnace_position)
        step = "verify_drop_tile"
        assert position_in_tile_footprint(drop, furnace["bounding_box"]), (
            f"drop={drop}, furnace={furnace_position}, bounding_box={furnace['bounding_box']}"
        )
        PAIRS.append((plate, position, furnace_position))
        step = "service"
        service()
    except Exception as error:
        print(
            f"build_pair failed: step={step}, plate={plate}, drill={position}, "
            f"furnace={furnace_position}, drop={drop}, station={STATION}: {error}",
            flush=True,
        )
        raise


def power_snapshot():
    observed = observe()
    layout = [
        ("offshore-pump", PUMP),
        ("boiler", BOILER),
        ("steam-engine", ENGINE),
        *(("small-electric-pole", p) for p in POLES),
        ("lab", LAB),
    ]
    snapshot = {}
    for name, position in layout:
        entity = entity_at(observed, name, position)
        snapshot[f"{name}@{position}"] = {
            key: entity.get(key) for key in ("status", "fluids", "electric_network_id")
        }
    LAST_STATUSES.update(snapshot)
    print("power", json.dumps(snapshot), flush=True)
    return observed


def main():
    global run_deadline
    run_deadline = time.monotonic() + 840
    pickup()
    fixture()
    recipes = Recipes()
    targets = {
        "offshore-pump": 1,
        "boiler": 1,
        "steam-engine": 1,
        "small-electric-pole": len(POLES),
        "lab": 1,
        "automation-science-pack": 10,
    }
    needs = recipes.raw_needs(targets)
    # Prototype fuel values: coal 4 MJ, drill 150 kW at .25 mining speed
    # (ore mining time .5 s), furnace 90 kW at crafting speed 1.
    # Include fuel held by the six machines, ten boiler coal, and a reserve.
    smelting_mj = sum(
        needs[p] * (0.09 * recipes.info(p)["energy"] + 0.15 * 2)
        for p in ("iron-plate", "copper-plate")
    )
    needs["coal"] += math.ceil(smelting_mj / 4) + 6 * 6 + 10 + 10
    print("computed needs:", json.dumps(dict(needs)), flush=True)
    print(
        "craftable recipes:", json.dumps(call("list_craftable_recipes", {"limit": 100})), flush=True
    )

    gather({"coal": 30}, run_deadline)
    build_pair("iron-plate", IRON_DRILLS[0])
    # Expand with plates produced by the first pair; no ore is hand fed.
    for plate, position in [("iron-plate", IRON_DRILLS[1]), ("copper-plate", COPPER_DRILL)]:
        bootstrap = recipes.raw_needs({"burner-mining-drill": 1, "stone-furnace": 1})
        bootstrap["coal"] = 20
        gather(bootstrap, time.monotonic() + 180)
        recipes.craft_to("burner-mining-drill", 1)
        recipes.craft_to("stone-furnace", 1)
        build_pair(plate, position)
    gather(needs, time.monotonic() + 360)
    report("materials ready", force=True)

    # Smelting has now unlocked steam-power/electronics through normal game
    # triggers. Crafting the lab unlocks the red science recipe in 2.0.77.
    for name in ("offshore-pump", "boiler", "steam-engine", "small-electric-pole", "lab"):
        recipes.craft_to(name, targets[name])
    research = call("get_research_status")
    assert "automation-science-pack" in (research["researched"] or []), (
        f"Lab hand-crafting did not unlock automation-science-pack: {research}"
    )
    print("lab craft trigger: automation-science-pack researched", flush=True)
    walk((20, -18))
    place("offshore-pump", PUMP, 12)
    place("boiler", BOILER)
    place("steam-engine", ENGINE)
    place("lab", LAB)
    assert all(math.dist(a, b) <= 7 for a, b in zip(POLES, POLES[1:], strict=False)), POLES
    for position in POLES:
        place("small-electric-pole", position)
    boiler = entity_at(observe(), "boiler", BOILER)
    transfer(boiler, "fuel", "coal", 10)

    deadline = time.monotonic() + 30
    while True:
        checked = power_snapshot()
        engine = entity_at(checked, "steam-engine", ENGINE)
        lab = entity_at(checked, "lab", LAB)
        steam = engine.get("fluids", [])
        if (
            isinstance(steam, list)
            and any(f["name"] == "steam" and f["amount"] > 0 for f in steam)
            and engine.get("electric_network_id") is not None
            and lab.get("electric_network_id") == engine["electric_network_id"]
            and engine.get("status") not in (None, "no_input_fluid", "no_power")
            and lab.get("status") not in (None, "no_power", "low_power")
        ):
            break
        check_deadline(deadline)
        time.sleep(2)

    recipes.craft_to("automation-science-pack", 10)
    lab = entity_at(observe(), "lab", LAB)
    transfer(lab, "input", "automation-science-pack", 10)
    call("start_research", {"technology": "automation"})
    deadline = time.monotonic() + 300
    while True:
        check_deadline(deadline)
        top_up("boiler", BOILER, threshold=3, target_count=10)
        research = bridge.call("get_research_status")["result"]
        if "automation" in (research["researched"] or []):
            power_snapshot()
            print("POWER_RESEARCH_SUCCEEDED", flush=True)
            return
        if time.monotonic() >= next_report:
            print("research", json.dumps(research), flush=True)
            power_snapshot()
        report("researching")
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, RuntimeError, TimeoutError, BridgeError):
        print(
            "power research failed:",
            json.dumps({"inventory": items(), "status": LAST_STATUSES}),
            flush=True,
        )
        raise
