# 51. Blue science reached, and fixes from trial 36 (M43)

## Result: chemical science automated (trials 34 to 36, seed 16180)

The blue-science goal was reached over three continued sessions on one unfamiliar map, starting from an empty inventory.

| Trial | Start | Length | Calls | Reached |
| --- | --- | --- | --- | --- |
| 34 | fresh map | about 85 min | 1,181 | power, iron line, assembler red and green science, logistics; stopped at the ungenerated map edge (fixed by M42) |
| 35 | trial 34 save | about 2.5 h | 1,967 | found crude oil about 830 tiles away; engine, fluid handling, oil gathering, oil processing, plastics, advanced circuit; ended by the agent's usage limit |
| 36 | trial 35 save | about 42 min | 333 | sulfur processing, chemical-science-pack, then advanced-oil-processing (75 each of red, green and blue) |

Audit of trial 36 (transcript):

- chemical-science-pack research started at call 190 and finished at call 251. advanced-oil-processing finished at call 331, consumed by five labs fed by inserters from two blue-science assemblers.
- After call 190 there was no `transfer_item` / `transfer_items` call with `to_entity` (the audit does find the earlier hand feeding at call 21).
- The only hand crafts in trial 36 were underground belts, splitters and wooden chests. No packs or blue intermediates were hand-crafted.

## Tool problems the AI reported in trial 36

1. `place_entity` refused `fast-inserter` with `invalid_target` even though it was researched and in the inventory: the Mod only accepts a fixed list of items.
2. `get_explored_map` does not show the factory's own buildings, so in a continued session the AI walked a long way to find its refinery.
3. A walking `place_line` in a narrow lab corridor stopped with `stand_position_not_found` (11 of 13 belts placed), although a plain move to a nearby spot then worked.

## DC: place and read by entity type

Items are accepted when their place result is a supported entity type, not only when the item name is on the list. Supported types: container, furnace, assembling-machine, inserter, transport-belt, underground-belt, splitter, electric-pole, pipe, pipe-to-ground, pump, storage-tank, mining-drill, boiler, generator, offshore-pump, lab, and the oil types already supported. This covers fast and bulk inserters, medium and big poles, steel furnaces and fast belts. Inventory roles follow the entity type in the same way.

## DD: own buildings in the explored map

Each explored cell also records the agent force's own machines seen there (name, count and one position), at observation time. Belts, pipes, poles and inserters are left out to keep the map small. Like everything else in the explored map, this is only what the character has observed.

## DE: line stand positions in narrow corridors

When no stand position is found for the next line tile, the Mod tries a wider search for a reachable standing spot within reach of that tile before giving up.
