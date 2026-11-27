# 50. Map generation around the character (M42)

## Trial 34 result (blue goal, seed 16180, from scratch)

The AI ran about 85 minutes (1,181 calls) and stopped by itself.

- Built: steam power, eight iron furnaces with belt-fed coal, assembler red and green science, and inserter feeding into two labs.
- Researched: automation, electric mining drills, logistic science, steel processing, automation 2 and logistics. Engine reached 6%.
- It surveyed 289 positions for crude oil and found none.
- Every move just past ±256 tiles, on all four sides, failed with `path_not_found`, even 1 to 1.5 tiles beyond the last reachable spot. `nearby_free_position` was the goal itself, so the diagnostics gave no clue.
- The AI stopped because it believed the map ended there.

Cause: the agent character is created by script and has no player attached. Factorio generates chunks around players, not around script characters. Only the starting area generated at map creation (±256 tiles, 8 chunks) ever existed. The pathfinder cannot route into ungenerated chunks. Crude oil is normally outside the starting area, so blue science was impossible on any seed.

A human player's walk generates the world around them. Doing the same for the agent character is ordinary game behaviour. Observation stays limited to the character's surroundings (invariant 5 unchanged).

## DA: generate chunks around the character

- The character's surroundings are requested for generation, like a player's: `surface.request_to_generate_chunks(position, 5)` at spawn and whenever the character enters a new chunk. A radius of 5 chunks covers move_to's 128-tile reach.
- Before a path request (move_to, walking batches, line approach, stand walks), if the goal's chunk is not generated, the Mod requests it with radius 1 and calls `surface.force_generate_chunk_requests()`, so the path is computed on a generated map.
- `path_not_found` detail adds `goal_chunk_generated` (boolean). When it is false, the error reports `reason = "not_generated"` with a hint to walk closer first.

## DB: partial place_line results

When a walking `place_line` ends with `status = "succeeded"` but `line_result.complete = false`, the result now carries top-level `partial = true`, `stopped` (reason) and a hint saying how many positions are left and where to continue.

## Prompt change for later trials

- The world generates around the character as it walks. Crude oil is usually outside the starting area; walk outward in steps of up to 128 tiles.
- Do not stop early: keep growing red and green production while the oil chain is missing.

## Trial 35 result (continued from trial 34's save, M42 code)

- About 2.5 hours and 1,967 calls. It ended when the agent's Codex usage limit was reached, not by the AI's choice.
- With chunks generated around the character, the AI explored far out and found crude oil about 830 tiles from the base (around x=294, y=-824).
- Researched in this run: engine, fluid handling, oil gathering, oil processing (the crude-oil mining trigger), plastics, advanced circuit and fast inserter. sulfur-processing was in progress at the end.
- No `not_generated` errors occurred. The remaining move errors were goals on deep water (`goal_tiles: ["deepwater"]`) and goals beyond the 128-tile reach.
- Other errors were the AI's own: starting research with missing prerequisites (8), reading inventories beyond the 10-tile reach (15) and crafts short of ingredients.
