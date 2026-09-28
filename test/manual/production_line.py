"""Manual integration test for a disposable save on RCON port 27277 only.

Creates raw-resource fixtures with /c. Never run against a real save.
"""

import json
import sys
import time
from pathlib import Path

from rcon.source import Client

from factorio_mcp.bridge import FactorioBridge


PASSWORD_FILE = Path(__file__).resolve().parents[2] / ".factorio-test" / "rcon-secret.txt"
password = PASSWORD_FILE.read_text(encoding="utf-8").strip()
bridge = FactorioBridge(port=27277, password=password)


def call(operation, params=None):
    result = bridge.call(operation, params or {})["result"]
    print(operation, json.dumps(result, ensure_ascii=False)[:600], flush=True)
    return result


def action(operation, params):
    started = call(operation, params)
    for _ in range(500):
        time.sleep(0.25)
        state = bridge.call("get_action_status", {"action_id": started["action_id"]})["result"]
        if state["status"] != "running":
            print(operation + "_done", json.dumps(state, ensure_ascii=False)[:400], flush=True)
            if state["status"] != "succeeded":
                raise RuntimeError(f"{operation}: {state.get('error')}")
            return state
    raise TimeoutError(operation)


def reference(name):
    observed = bridge.call("observe_nearby")["result"]
    for entity in observed["entities"]:
        if entity["name"] == name:
            return entity["target_ref"]
    raise RuntimeError(f"{name} not visible")


def mine(name, count):
    return action("mine_entity", {"target_ref": reference(name), "count": count})


def transfer(name, role, item, count):
    return call("transfer_item", {
        "target_ref": reference(name), "direction": "to_entity",
        "inventory_role": role, "item": item, "count": count,
    })


def inventory(name):
    return bridge.call("get_entity_inventory", {"target_ref": reference(name)})["result"]


def amount(items, name):
    if not isinstance(items, list):
        return 0
    return next((item["count"] for item in items if item["name"] == name), 0)


def main():
    if "--resume" not in sys.argv:
        fixture = ('/c local s=game.surfaces.nauvis;local n=0;for _,v in '
                   'ipairs({{"stone",2,3},{"coal",3,5},{"iron-ore",5,5},'
                   '{"iron-ore",6,5},{"iron-ore",5,6},{"iron-ore",6,6}}) do '
                   'if s.create_entity{name=v[1],position={v[2],v[3]},amount=100} '
                   'then n=n+1 end end;if s.create_entity{name="tree-01",position={3,1}} '
                   'then n=n+1 end;rcon.print("fixture:"..n)')
        with Client("127.0.0.1", 27277, passwd=password, timeout=5) as client:
            result = client.run(fixture)
            if "fixture:" not in result:
                result = client.run(fixture)
            print(result.strip(), flush=True)

        action("move_to", {"surface": "nauvis", "x": 2, "y": 2.5})
        mine("stone", 10)
        mine("tree-01", 1)
        action("move_to", {"surface": "nauvis", "x": 4, "y": 4})
        mine("coal", 3)
        mine("iron-ore", 9)
        call("get_inventory")
        action("craft", {"recipe": "stone-furnace", "count": 2})

    call("observe_nearby")
    call("place_entity", {"item": "stone-furnace", "surface": "nauvis", "x": 6, "y": 4, "direction": 0})
    transfer("stone-furnace", "fuel", "wood", 2)
    transfer("stone-furnace", "source", "iron-ore", 9)
    for i in range(70):
        time.sleep(1)
        plates = amount(inventory("stone-furnace")["inventories"]["result"], "iron-plate")
        if i % 5 == 0:
            print("bootstrap_plates", plates, flush=True)
        if plates >= 9:
            break
    else:
        raise RuntimeError(f"Only {plates} bootstrap plates")
    call("transfer_item", {
        "target_ref": reference("stone-furnace"), "direction": "from_entity",
        "inventory_role": "result", "item": "iron-plate", "count": 9,
    })
    action("craft", {"recipe": "iron-gear-wheel", "count": 3})
    action("craft", {"recipe": "burner-mining-drill", "count": 1})
    call("get_inventory")
    call("place_entity", {"item": "burner-mining-drill", "surface": "nauvis", "x": 6, "y": 6, "direction": 0})
    print("drill", json.dumps(inventory("burner-mining-drill")), flush=True)
    transfer("burner-mining-drill", "fuel", "coal", 2)
    transfer("stone-furnace", "fuel", "coal", 1)
    for i in range(90):
        time.sleep(1)
        plates = amount(inventory("stone-furnace")["inventories"]["result"], "iron-plate")
        if i % 5 == 0:
            print("production_plates", plates, flush=True)
        if plates >= 10:
            print("PRODUCTION_LINE_SUCCEEDED", plates, flush=True)
            return
    raise RuntimeError(f"Only {plates} automated plates")


if __name__ == "__main__":
    main()
