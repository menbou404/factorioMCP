"""STDIO MCP tools for one Factorio AI character."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, InputRequiredResult, TextContent
from pydantic import BaseModel, Field, ValidationError

from .bridge import BridgeError, FactorioBridge

NonNegative = Annotated[int, Field(ge=0, strict=True)]
Count = Annotated[int, Field(ge=1, le=100, strict=True)]
RequiredText = Annotated[str, Field(min_length=1)]
Coordinate = Annotated[float, Field(allow_inf_nan=False)]
ObservationRadius = Annotated[float, Field(ge=1, le=16, allow_inf_nan=False, strict=True)]
MoveWithin = Annotated[float, Field(ge=0.5, le=10, allow_inf_nan=False, strict=True)]
FilterNames = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=200, strict=True)]],
    Field(max_length=32, strict=True),
]


TARGET_REF_DESCRIPTION = (
    "Owned-structure target refs (your force, with a unit_number) are stable across observations "
    "and do not expire while the entity exists. Other refs expire after 60 seconds. "
    "Reach, surface, and force checks still apply."
)


PLACE_DESCRIPTION = (
    "Place a supported entity within build reach, consuming one real inventory item."
    " Supports underground-belt, splitter, long-handed-inserter, and pipe-to-ground."
    " underground_type is only valid for underground-belt: input or output"
    " (default input). direction is belt travel direction for both ends;"
    " for output it is the direction items leave."
    " The placement area must be covered by any of the last 8 observations"
    " within 60 seconds."
    " Items on the ground inside the footprint are picked up into the main inventory"
    " in the same tick before building when they are the only blockers and all fit."
    " collected_items lists the picked-up stacks as {name, quality, count}, sorted by name."
    " If they do not all fit, nothing is collected: detail.reason is"
    " ground_items_inventory_full and detail.ground_items lists the stacks; free inventory space."
    " Item-entity blocking entries include stack={name, quality, count}."
    " If placement fails after collection, detail.collected_items reports the items kept."
    " Returns target_ref for the placed entity, the snapped position and actual"
    " fluid_connections with port position,"
    " target_position, direction, flow, fluidbox_index, and connected when present."
    " For offshore-pump, the water_direction side must face water; pipes/boilers"
    " connect on the output_direction side (see check_placement or observe_nearby)."
    " Blocked errors may include valid_directions for another orientation."
    " Each detail.blocking entry includes a target_ref usable with mine_entity. "
    + TARGET_REF_DESCRIPTION
    + " detail.reason is character_in_the_way when the character blocks placement"
    " (move the character away first), or placement_rules when no blocking entities,"
    " tiles, or character explain the failure."
    " For placement_rules, detail.nearby_positions lists up to 3 placeable centres"
    " and directions within 8 tiles of the requested position."
    " They may be out of reach; move closer and check again with check_placement."
    " For mining drills, detail.mining_area_resources lists mineable resources as {name, tiles}"
    " in the snapped mining area, sorted by name; an empty list also sets"
    " detail.reason_detail to no_resources_in_mining_area."
)

TRANSFER_DESCRIPTION = (
    "Move a real item between the AI character and a nearby observed structure. "
    "count is 1-100. "
    "Success returns moved and requested (= count). If moved < requested, partial=true, "
    "limited_by (source or destination), available (source count before the move), and "
    "insertable (destination capacity before the move) explain the shortfall; "
    "equal binding limits prefer source. Full transfers omit these four partial fields. "
    "Choose inventory_role from the structure's inventory_roles in observe_nearby. "
    "Role aliases: stone-furnace and assembling-machine-1 accept input -> source "
    "and output -> result; lab accepts source -> input. Returned roles stay canonical. "
    "missing_materials detail includes item, requested, available (0), and contents "
    "of the source inventory (up to 10 items sorted by name, omitted when empty). "
    "inventory_role is included when taking items from the structure, along with status "
    "when available and recipe for assemblers/furnaces (idle furnaces use previous_recipe). "
    "For an empty entity source, fix no_fuel, no_power, item_ingredient_shortage, or "
    "no_ingredients first, then wait with wait_for_items. "
    "item_not_accepted means the entity rejects the item: assembler source accepts only "
    "current recipe ingredients; detail includes item, entity, inventory_role, recipe, and "
    "ingredients (names), or reason=no_recipe when unset. Insert only detail.ingredients "
    "or change the recipe with set_recipe. Furnace source accepts ore/smeltable items from "
    "recipes in its crafting categories, including locked recipes; otherwise "
    "reason=no_smelting_recipe. Rejected items have insertable=0. "
    "inventory_full detail describes the destination inventory: item, requested, "
    "destination (entity or character), inventory_role (entity only), "
    "contents (up to 10 {name, quality, count} entries sorted by name then quality), "
    "slots, empty_slots, and insertable count for the item. "
    "For accepted items, insertable uses the inventory capacity estimate; Factorio may "
    "overestimate assembler filtered slots. moved is the actual inserted count."
)

MACHINE_STATUS_DESCRIPTION = (
    " status uses the same names as observe_nearby and is omitted when the entity has no status."
    " full_output means the result inventory is full and the machine stopped;"
    " take products out (transfer from result) or add an output inserter."
    " waiting_for_space_in_destination for inserters means their target is full."
)

INVENTORY_RECIPE_DESCRIPTION = (
    " Assemblers and furnaces include recipe when available;"
    " idle furnaces use their previous recipe. Omitted when no recipe is available."
)

RESEARCH_STALLED_HINT = (
    "Check missing_science in observe_nearby: every lab with packs must hold every required "
    "science pack. For each ingredient, check that the sum of science_units across labs "
    "exceeds remaining_units. Partly used packs stay in the lab that started them; "
    "labs working in parallel each need a pack of every ingredient. Check lab power too."
)

# Keep in sync with ROLE_ALIASES in control.lua; returned roles are canonical.
ROLE_ALIASES = {
    "stone-furnace": {"input": "source", "output": "result"},
    "assembling-machine-1": {"input": "source", "output": "result"},
    "lab": {"source": "input"},
}

# Stopped states from Factorio 2.0.77 defines.entity_status. In particular,
# low_power is not stopped: a machine may still make progress at reduced speed.
ITEMS_STALLED_HINTS = {
    "full_output": "Take products out or add an output inserter.",
    "full_burnt_result_output": "Take spent fuel out of the burnt result inventory.",
    "no_ingredients": "Supply materials to the machine's input inventory.",
    "item_ingredient_shortage": "Supply missing materials to the machine's input inventory.",
    "fluid_ingredient_shortage": "Supply the missing ingredient fluids.",
    "no_input_fluid": "Supply the required input fluid.",
    "missing_required_fluid": "Supply the required mining fluid.",
    "missing_science_packs": "Supply the required science packs to the lab.",
    "no_fuel": "Supply fuel to the machine's fuel inventory.",
    "no_power": "Connect the machine to a working power supply.",
    "no_recipe": "Set a recipe on the assembler.",
    "no_research_in_progress": "Start research for the lab to work on.",
    "no_minable_resources": "Place the drill over mineable resources.",
    "waiting_for_source_items": "Supply materials at the inserter's pickup position.",
    "waiting_for_space_in_destination": "Free space in the output destination.",
    "disabled_by_control_behavior": "Check the machine's circuit control conditions.",
    "disabled_by_script": "Check why the machine was disabled by a script.",
    "marked_for_deconstruction": "Cancel deconstruction to let the machine work.",
}


class TransferEntry(BaseModel):
    target_ref: RequiredText
    direction: Literal["to_entity", "from_entity"]
    inventory_role: RequiredText
    item: RequiredText
    count: Count


class PlaceEntry(BaseModel):
    item: RequiredText
    surface: RequiredText
    x: Coordinate
    y: Coordinate
    direction: Literal[0, 4, 8, 12] = 0
    underground_type: Literal["input", "output"] | None = None


HINTS = {
    "invalid_input": (
        "Check the tool's input schema and argument values. "
        "When present, detail.errors lists each invalid field."
    ),
    "target_gone": (
        TARGET_REF_DESCRIPTION
        + " For reason=expired, refresh with observe_nearby; reason=entity_gone means "
        "the entity was removed or destroyed."
    ),
    "not_visible": (
        "Observe the placement area before placing."
        " Any of the last 8 observations within 60 seconds counts."
    ),
    "action_conflict": (
        "Wait for detail.running_action with wait_for_action, or stop it with cancel_action. "
        "Only one craft at a time and one walk/mine at a time; crafting can run alongside either. "
        "place_entity, transfer_item, rotate_entity, and set_recipe also return action_conflict "
        "while a walk or mine is running; they are allowed while crafting."
    ),
    "path_not_found": (
        "Check detail.goal_tiles and detail.goal_entities for obstacles explaining the failure. "
        "detail.start_adjusted, when present, is the nearby pathfinding start used because "
        "the character was touching an obstacle, or a tile-centre start within 2 tiles "
        "used for a once-per-move escape retry after a failed search; "
        "the character still moves by walking. "
        "If present, detail.nearby_free_position is a standable spot to try with move_to; "
        "there is no path guarantee. To reach a structure among buildings, retry with "
        "within set to the interaction reach (reach.interact from observe_nearby)."
    ),
    "blocked": (
        "Check valid_directions for another orientation, mine or recover blocking entities, "
        "or find another position with check_placement. "
        "If reason is character_in_the_way, move the character away first. "
        "For ground_items_inventory_full, free inventory space for ground_items and retry. "
        "For placement_rules, nearby_positions lists up to 3 placeable centres and directions "
        "within 8 tiles of the requested position. They may be out of reach; "
        "move closer and check again with check_placement."
        " For mining drills, mining_area_resources lists mineable resources as {name, tiles};"
        " reason_detail=no_resources_in_mining_area means move the drill onto suitable ore."
    ),
    "invalid_target": "Check the target, detail, and the tool's supported targets before retrying.",
    "out_of_range": (
        "Use a target on the character's current surface. "
        "Move within detail.reach of the target, then observe_nearby again."
    ),
    "missing_materials": (
        "Gather the missing items before retrying. "
        "For crafting, detail.missing_raw lists raw items such as plates that are short "
        "after expanding hand-craftable intermediates and using held materials first. "
        "For transfers, check detail.contents for items in the source inventory. "
        "For an entity source, check detail.status and detail.recipe: fix no_fuel (supply fuel), "
        "no_power (restore power), or item_ingredient_shortage/no_ingredients (supply recipe "
        "ingredients) first, then wait with wait_for_items before taking products."
    ),
}


INVENTORY_TARGET_HINT = (
    "Choose an owned supported structure; check detail.reason and supported_entities. "
    "Check the entity, valid_roles, and role_aliases in detail. "
    "Furnaces and assemblers accept input -> source and output -> result; "
    "labs accept source -> input."
)

OPERATION_HINTS = {
    ("get_technology_info", "invalid_target"): (
        "Check detail.technology: detail.reason=unknown means that internal technology name "
        "does not exist in the character's force. Use a technology name from get_research_status."
    ),
    ("start_research", "invalid_target"): (
        "Check detail.reason. For missing_prerequisites, research "
        "detail.missing_prerequisites first; get_research_status lists available technologies."
    ),
    ("move_to", "out_of_range"): (
        "The goal must be on the current surface and within detail.reach (128) tiles "
        "in a straight line. Walk via intermediate points for farther goals."
    ),
    ("transfer_item", "invalid_target"): INVENTORY_TARGET_HINT,
    ("transfer_item", "item_not_accepted"): (
        "For an assembler, insert only the recipe ingredients in detail.ingredients or "
        "change the recipe with set_recipe; detail.reason=no_recipe means set a recipe first. "
        "For a furnace, insert ore/smeltable items into source; "
        "detail.reason=no_smelting_recipe means no recipe in its crafting categories "
        "uses that item. "
        "For other roles, choose an item the destination accepts."
    ),
    ("transfer_item", "inventory_full"): (
        "detail describes the destination inventory; check destination, inventory_role, "
        "contents, slots, empty_slots, and insertable. "
        "When a fuel/input slot holds a different item, take it out with direction "
        "from_entity first (or use another target). "
        "When the character is full, free inventory space."
    ),
    ("get_entity_inventory", "invalid_target"): INVENTORY_TARGET_HINT,
    ("mine_entity", "invalid_target"): (
        "Choose a minable, selectable target in observe_nearby; "
        "check detail.reason and avoid structures belonging to another force."
    ),
    ("rotate_entity", "invalid_target"): (
        "Choose an owned rotatable entity in observe_nearby; check detail.reason."
    ),
    ("set_recipe", "invalid_target"): "Choose an owned assembling machine in observe_nearby.",
    ("set_recipe", "inventory_full"): (
        "Free main inventory space for detail.items ({name, quality, count}); "
        "detail.empty_slots reports available unfiltered slots. Capacity is checked "
        "conservatively before changing anything. Retry to return inputs, outputs, and "
        "in-progress ingredients to the character as returned_items."
    ),
    ("set_recipe", "not_empty"): (
        "detail.reason=modules means remove modules first. Inputs, outputs, and an "
        "in-progress craft do not prevent recipe changes; their items are returned "
        "to the main inventory as returned_items."
    ),
    ("get_recipe_info", "invalid_target"): (
        "Check the recipe name; list_craftable_recipes lists unlocked hand crafting recipes."
    ),
    ("place_entity", "invalid_target"): "Choose an item supported by place_entity.",
}


def _hint(operation: str | None, code: str | None) -> str | None:
    return OPERATION_HINTS.get((operation, code), HINTS.get(code))


def _normalize_error_detail(detail: dict[str, Any]) -> dict[str, Any]:
    detail = detail.copy()
    if "role_aliases" in detail and not isinstance(detail["role_aliases"], dict):
        detail["role_aliases"] = {}
    # Factorio serializes empty Lua arrays as objects.
    for field in (
        "blocking",
        "tiles",
        "missing",
        "missing_raw",
        "missing_prerequisites",
        "valid_roles",
        "supported_entities",
        "contents",
        "mining_area_resources",
        "ground_items",
        "collected_items",
        "items",
        "ingredients",
    ):
        if field in detail and not isinstance(detail[field], list):
            detail[field] = []
    return detail


def _normalize_batch_result(result: dict[str, Any], operation: str | None = None) -> dict[str, Any]:
    operation = operation or result.get("operation")
    if not isinstance(result.get("results"), list):
        result["results"] = []
    for entry in result["results"]:
        if entry.get("ok") is True and isinstance(entry.get("result"), dict) and operation:
            entry["result"] = _normalize_result(operation, entry["result"])
        elif entry.get("ok") is False:
            if isinstance(entry.get("detail"), dict):
                entry["detail"] = _normalize_error_detail(entry["detail"])
            hint = _hint(operation, entry.get("error"))
            if hint is not None:
                entry["hint"] = hint
    return result


def _normalize_result(operation: str, result: dict[str, Any]) -> dict[str, Any]:
    batch_operations = {
        "transfer_items": "transfer_item",
        "place_entities": "place_entity",
        "get_entity_inventories": "get_entity_inventory",
    }
    if operation in batch_operations:
        return _normalize_batch_result(result, batch_operations[operation])
    if operation == "get_request_result":
        original = result.get("original_response")
        if isinstance(original, dict):
            if isinstance(original.get("detail"), dict):
                original["detail"] = _normalize_error_detail(original["detail"])
            if isinstance(original.get("result"), dict):
                cached = original["result"]
                if "results" in cached and "succeeded" in cached and "failed" in cached:
                    _normalize_batch_result(cached)
                else:
                    _normalize_result("", cached)
    if "role_aliases" in result and not isinstance(result["role_aliases"], dict):
        result["role_aliases"] = {}
    for field in ("collected_items", "ground_items", "returned_items", "spilled_items"):
        if field in result and not isinstance(result[field], list):
            result[field] = []
    array_fields = {
        "observe_nearby": ("entities", "blocked_tiles", "resources"),
        "get_explored_map": ("resources", "water", "trees", "rocks"),
        "get_inventory": ("items",),
        "get_agent_status": ("inventory",),
        "list_craftable_recipes": ("recipes",),
        "get_research_status": ("researched", "queue", "available"),
        "get_technology_info": ("prerequisites", "ingredients", "unlocks_recipes"),
        "start_research": ("queue",),
        "get_recipe_info": ("ingredients", "products"),
    }
    for field in array_fields.get(operation, ()):
        if not isinstance(result.get(field), list):
            result[field] = []
    if operation == "observe_nearby":
        filters = result.get("filters")
        if isinstance(filters, dict):
            for field in ("types", "names"):
                if field in filters and not isinstance(filters[field], list):
                    filters[field] = []
        for entity in result["entities"]:
            for field in (
                "belt_items",
                "fluids",
                "inventory_roles",
                "missing_science",
                "science_units",
            ):
                if field in entity and not isinstance(entity[field], list):
                    entity[field] = []
    if operation == "check_placement":
        for field in ("blocking", "tiles", "mining_area_resources"):
            if field in result and not isinstance(result[field], list):
                result[field] = []
        coverage = result.get("electric_coverage")
        if isinstance(coverage, dict) and not isinstance(coverage.get("poles"), list):
            coverage["poles"] = []
    if operation == "get_research_status":
        for technology in result["available"]:
            if not isinstance(technology.get("ingredients"), list):
                technology["ingredients"] = []
    if operation == "get_entity_inventory" and isinstance(result.get("inventories"), dict):
        for role, items in result["inventories"].items():
            if not isinstance(items, list):
                result["inventories"][role] = []
    return result


def _response(value: dict[str, Any], *, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(text=json.dumps(value, ensure_ascii=False))], isError=error
    )


def _error_response(exc: Exception, operation: str | None = None) -> CallToolResult:
    code = exc.code if isinstance(exc, BridgeError) else "internal_error"
    detail = exc.detail if isinstance(exc, BridgeError) else None
    value: dict[str, Any] = {"ok": False, "error": code, "message": str(exc)}
    if isinstance(detail, dict):
        if detail.get("request_id") is not None:
            value["request_id"] = detail["request_id"]
        public_detail = _normalize_error_detail(
            {key: item for key, item in detail.items() if key != "request_id"}
        )
        if public_detail:
            value["detail"] = public_detail
    hint = _hint(operation, code)
    if hint is not None:
        value["hint"] = hint
    return _response(value, error=True)


def _success_value(operation: str, response: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "tick": response.get("tick"),
        **_normalize_result(operation, response.get("result") or {}),
    }


class _InputErrorServer(MCPServer):
    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context | None = None
    ) -> CallToolResult | InputRequiredResult:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            # Tool.run wraps argument validation directly in ToolError. Body/output
            # validation failures use UnexpectedToolError; nested errors have another
            # wrapper as their cause. Leave both on the SDK's existing error path.
            if isinstance(exc, UnexpectedToolError) or not isinstance(
                exc.__cause__, ValidationError
            ):
                raise
            errors = [
                {
                    "field": ".".join(str(part) for part in error["loc"]),
                    "type": error["type"],
                    "message": error["msg"],
                }
                for error in exc.__cause__.errors(
                    include_url=False, include_context=False, include_input=False
                )
            ]
            result = _error_response(
                BridgeError(
                    "invalid_input", "Tool arguments failed validation.", {"errors": errors}
                ),
                name,
            )
            # Preserve call_tool's exception contract. The SDK's wire handler turns
            # this message into a text tool result with isError=true.
            raise ToolError(result.content[0].text) from exc


def create_server(bridge: FactorioBridge | None) -> MCPServer:
    server = _InputErrorServer("factorio-ai", version="0.1.0")

    async def call(operation: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if bridge is None:
            raise BridgeError(
                "not_configured", "Set FACTORIO_RCON_PASSWORD before starting the MCP server."
            )
        return await asyncio.to_thread(bridge.call, operation, params)

    async def invoke(operation: str, params: dict[str, Any] | None = None) -> CallToolResult:
        try:
            return _response(_success_value(operation, await call(operation, params)))
        except Exception as exc:
            return _error_response(exc, operation)

    async def wait_action(
        action_id: str, timeout_seconds: float, start_value: dict[str, Any] | None = None
    ) -> CallToolResult:
        started = time.monotonic()
        try:
            while True:
                response = await call("get_action_status", {"action_id": action_id})
                value = _success_value("get_action_status", response)
                waited = time.monotonic() - started
                running = value.get("status") == "running"
                if not running or waited >= timeout_seconds:
                    return _response(
                        {
                            **(start_value or {}),
                            **value,
                            "timed_out": running,
                            "waited_seconds": waited,
                        }
                    )
                await asyncio.sleep(min(0.25, timeout_seconds - waited))
        except Exception as exc:
            return _error_response(exc)

    async def start_action(
        operation: str, params: dict[str, Any], wait_seconds: float | None
    ) -> CallToolResult:
        try:
            value = _success_value(operation, await call(operation, params))
        except Exception as exc:
            return _error_response(exc, operation)
        if wait_seconds is None or value.get("status") in {"succeeded", "failed", "cancelled"}:
            return _response(value)
        return await wait_action(value["action_id"], wait_seconds, value)

    @server.tool(description="Check the connection to the Factorio AI Mod.")
    async def factorio_ping() -> CallToolResult:
        return await invoke("ping")

    @server.tool(
        description=(
            "Get the AI character position, health, inventory, and current actions. "
            "action is the walk/mine slot; craft_action is the crafting slot."
        )
    )
    async def get_agent_status() -> CallToolResult:
        return await invoke("get_agent_status")

    @server.tool(
        description=(
            "Observe nearby entities and blocked terrain tiles within 16 tiles. "
            "Each entity includes distance from the character centre to the entity centre. "
            "Entities are nearest first, with ties ordered by x, y, then name; "
            "entity_offset pages follow this order. "
            "reach gives build (placing), interact (transferring, rotating, and setting recipes), "
            "and mine_resource (resource/tree/rock mining) reach in tiles. "
            "Resources, trees, and rocks are judged to their collision edge, so they may be "
            "reachable at a slightly larger distance than mine_resource. "
            "With include_resources = false, individual resource entities are omitted but the "
            "resources summary still returns per-resource tile count, total amount, "
            "and bounding area. "
            + TARGET_REF_DESCRIPTION
            + " Placement observations expire after 60 seconds. "
            "Supported structures in your force include sorted inventory_roles. "
            "During current research, owned labs include sorted missing_science names "
            "for required science packs absent from their input inventory; "
            "omitted when nothing is missing or there is no current research. "
            "Owned labs with packs also include science_units as {name, units}, sorted by name: "
            "remaining science per pack type, including partly used packs, rounded to 3 decimals "
            "(a full pack is 1 unit); omitted when the lab input is empty. "
            "Owned fluid structures include fluid_connections with port position, target_position, "
            "direction, flow, fluidbox_index, connected, "
            "and connection_type (normal or underground). "
            "Transport belts, underground-belt, and splitter include belt_items as "
            "{name, quality, count} entries merged across all transport lines. "
            "Underground belts include underground_type and, when paired, neighbour = {x, y}. "
            "Inserters, including long-handed-inserter, include pickup_position and drop_position. "
            "Use check_placement for snapped position and valid_directions before building. "
            "Follow offsets for additional pages."
            " Each call also records fully observed 8x8 cells for get_explored_map, "
            "independent of filters and paging." + MACHINE_STATUS_DESCRIPTION
        )
    )
    async def observe_nearby(
        entity_offset: NonNegative = 0,
        tile_offset: NonNegative = 0,
        radius: ObservationRadius = 16,
        types: FilterNames | None = None,
        names: FilterNames | None = None,
        owned_only: Annotated[bool, Field(strict=True)] = False,
        include_resources: Annotated[bool, Field(strict=True)] = True,
        include_tiles: Annotated[bool, Field(strict=True)] = True,
    ) -> CallToolResult:
        params: dict[str, Any] = {"entity_offset": entity_offset, "tile_offset": tile_offset}
        if radius != 16:
            params["radius"] = radius
        if types is not None:
            params["types"] = types
        if names is not None:
            params["names"] = names
        if owned_only:
            params["owned_only"] = owned_only
        if not include_resources:
            params["include_resources"] = include_resources
        if not include_tiles:
            params["include_tiles"] = include_tiles
        return await invoke("observe_nearby", params)

    @server.tool(
        description=(
            "Read only what this character saw with observe_nearby on the current surface: "
            "8x8 tiles per cell, recorded only when fully inside an observation circle. "
            "Values are as of observed_tick and update when the area is observed again. "
            "Lists resource patches, water (with a shore tile for offshore pumps), forests "
            "and rocks nearest first. Each list contains at most limit components; "
            "resources_total, water_total, trees_total and rocks_total count components "
            "before truncation. Use resource_names to filter resource kinds."
        )
    )
    async def get_explored_map(
        limit: Annotated[int, Field(ge=1, le=50, strict=True)] = 10,
        resource_names: FilterNames | None = None,
    ) -> CallToolResult:
        params: dict[str, Any] = {"limit": limit}
        if resource_names is not None:
            params["resource_names"] = resource_names
        return await invoke("get_explored_map", params)

    @server.tool(description="Read the AI character’s actual in-game inventory.")
    async def get_inventory() -> CallToolResult:
        return await invoke("get_inventory")

    @server.tool(
        description=(
            "Page through unlocked hand crafting recipes, ingredients, "
            "and current craftable counts."
        )
    )
    async def list_craftable_recipes(
        offset: NonNegative = 0, limit: Annotated[int, Field(ge=1, le=100, strict=True)] = 50
    ) -> CallToolResult:
        return await invoke("list_craftable_recipes", {"offset": offset, "limit": limit})

    @server.tool(
        description=(
            "Read technologies researched by the AI character’s force and current research. "
            "Current research includes progress and remaining_units = unit_count * (1 - progress), "
            "using the force technology's current unit count, including count-formula costs. "
            "remaining_units is omitted when nothing is being researched."
        )
    )
    async def get_research_status() -> CallToolResult:
        return await invoke("get_research_status")

    @server.tool(
        description=(
            "Read a technology in the AI character's force, even before it is available. "
            "Returns name, researched, available, enabled, prerequisites with researched flags, "
            "unit_count, unit_time in seconds, ingredients as {name, amount}, unlocks_recipes, "
            "and trigger metadata when present (same shape as get_research_status). "
            "available requires enabled, unresearched, and all prerequisites researched. "
            "Read-only; unknown names return invalid_target with detail.reason=unknown."
        )
    )
    async def get_technology_info(technology: RequiredText) -> CallToolResult:
        return await invoke("get_technology_info", {"technology": technology})

    @server.tool(
        description=(
            "Wait for a technology to finish research. Returns early if it is not queued. "
            "Prefer this over polling get_research_status. The maximum wait is 50 seconds "
            "to keep it under common 60-second client tool timeouts."
            " Returns early with stalled=true, stalled_seconds, progress, and timed_out=false "
            "when this technology is current and its progress has not changed for 10 seconds "
            "of waiting. Otherwise stalled=false. Includes remaining_units when this technology "
            "is current, as returned by get_research_status. On a stall: " + RESEARCH_STALLED_HINT
        )
    )
    async def wait_for_research(
        technology: RequiredText,
        timeout_seconds: Annotated[float, Field(ge=1, le=50, allow_inf_nan=False)] = 20,
    ) -> CallToolResult:
        started = time.monotonic()
        unchanged_since = None
        last_progress = None
        try:
            while True:
                response = await call("get_research_status")
                value = _success_value("get_research_status", response)
                researched = technology in value["researched"]
                current = value.get("current") == technology
                researching = current or technology in value["queue"]
                now = time.monotonic()
                waited = now - started
                progress = value.get("progress") if current else None
                if current and progress is not None and not researched:
                    if unchanged_since is None or progress != last_progress:
                        unchanged_since = now
                    last_progress = progress
                else:
                    unchanged_since = None
                    last_progress = None
                stalled_seconds = now - unchanged_since if unchanged_since is not None else 0
                stalled = stalled_seconds >= 10
                if researched or not researching or stalled or waited >= timeout_seconds:
                    result = {
                        "technology": technology,
                        "researched": researched,
                        "researching": researching,
                        "progress": progress,
                        "stalled": stalled,
                        "timed_out": not researched
                        and researching
                        and not stalled
                        and waited >= timeout_seconds,
                        "waited_seconds": waited,
                    }
                    if current and "remaining_units" in value:
                        result["remaining_units"] = value["remaining_units"]
                    if stalled:
                        result["stalled_seconds"] = stalled_seconds
                        result["hint"] = RESEARCH_STALLED_HINT
                    return _response(result)
                await asyncio.sleep(min(0.25, timeout_seconds - waited))
        except Exception as exc:
            return _error_response(exc)

    @server.tool(
        description="Read a force recipe, its materials, products, and hand crafting availability."
    )
    async def get_recipe_info(recipe: RequiredText) -> CallToolResult:
        return await invoke("get_recipe_info", {"recipe": recipe})

    @server.tool(
        description=(
            "Set an enabled recipe on an owned assembling machine within reach. "
            "Inputs, outputs, and ingredients of an in-progress craft return to the character's "
            "main inventory as returned_items ({name, quality, count}). "
            "A conservative capacity check may require empty unfiltered slots even when "
            "existing stacks have room. If they do not fit, inventory_full returns detail.items "
            "and detail.empty_slots without changing the recipe, contents, or craft. "
            "Items are not spilled normally; spilled_items reports an unexpected insert shortfall. "
            "Modules still cause not_empty with detail.reason=modules. "
            "Setting the same recipe is a no-op success."
        )
    )
    async def set_recipe(target_ref: RequiredText, recipe: RequiredText) -> CallToolResult:
        return await invoke("set_recipe", {"target_ref": target_ref, "recipe": recipe})

    @server.tool(
        description=(
            "Queue an available technology for lab research consuming science packs. "
            "On invalid_target, detail.technology and detail.reason explain the rejection: "
            "unknown, researched, missing_prerequisites, or not_enabled. "
            "missing_prerequisites includes sorted direct prerequisite names in "
            "detail.missing_prerequisites; research these first. "
            "queue_rejected means the engine declined to queue an otherwise available technology. "
            "get_research_status lists available technologies."
        )
    )
    async def start_research(technology: RequiredText) -> CallToolResult:
        return await invoke("start_research", {"technology": technology})

    @server.tool(
        description=(
            "Walk to a position on the current surface. By default, arrival is within 0.5 tiles. "
            "Set within (0.5-10 tiles) to stop when the character centre is within that "
            "Euclidean distance of the goal; the pathfinder uses the same radius. "
            "Use within = the needed reach (e.g. reach.interact from observe_nearby) to walk "
            "next to a structure surrounded by other buildings. "
            "When set, completion detail includes within and the final distance. "
            "The goal must be within 128 tiles in a straight line from the character. "
            "For farther goals, move via intermediate points. "
            "If the goal is blocked, succeeds within 1.5 tiles with detail.goal_blocked = true "
            "and detail.distance. Blocked or timeout failures include detail.distance and "
            "detail.goal. path_not_found includes goal details and may include goal_tiles, "
            "goal_entities, and nearby_free_position (a standable spot with no path guarantee). "
            "start_adjusted, when present, is the nearby pathfinding start used when touching "
            "an obstacle, or a tile-centre start within 2 tiles used for a once-per-move "
            "escape retry after a failed search; the character still moves by walking. "
            "Crafting runs alongside walking. Only one walk/mine at a time. "
            "Prefer wait_seconds (1-50) to start and wait in one call, under common 60-second "
            "client timeouts. A timeout leaves the action running and returns timed_out=true "
            "and action_id; continue with wait_for_action. Omit wait_seconds to return the "
            "start result immediately."
        )
    )
    async def move_to(
        surface: RequiredText,
        x: Coordinate,
        y: Coordinate,
        within: MoveWithin | None = None,
        wait_seconds: Annotated[float, Field(ge=1, le=50, allow_inf_nan=False)] | None = None,
    ) -> CallToolResult:
        params: dict[str, Any] = {"surface": surface, "x": x, "y": y}
        if within is not None:
            params["within"] = within
        return await start_action("move_to", params, wait_seconds)

    @server.tool(
        description=(
            "Mine an observed entity within reach over normal game time. Returns an action ID. "
            "count is 1-100. "
            "When computable, estimated_seconds in the start result and running detail is an "
            "estimate that can change if mining is interrupted or the target is depleted. "
            "Use it to pick wait_for_action timeout_seconds (maximum 50). "
            "Crafting runs alongside mining. Only one walk/mine at a time. "
            "While mining runs, place_entity, transfer_item, rotate_entity, and set_recipe "
            "return action_conflict."
        )
    )
    async def mine_entity(target_ref: RequiredText, count: Count = 1) -> CallToolResult:
        return await invoke("mine_entity", {"target_ref": target_ref, "count": count})

    @server.tool(
        description=(
            "Start normal hand crafting using the AI character’s own materials. "
            "count is 1-100. "
            "Crafting runs alongside walking and mining. Only one craft at a time. "
            "Prefer wait_seconds (1-50) to start and wait in one call, under common 60-second "
            "client timeouts. A timeout leaves the action running and returns timed_out=true "
            "and action_id; continue with wait_for_action. Omit wait_seconds to return the "
            "start result immediately. On missing_materials, detail.missing_raw lists "
            "raw items such as plates that are short after expanding hand-craftable "
            "intermediates and using held materials first."
        )
    )
    async def craft(
        recipe: RequiredText,
        count: Count,
        wait_seconds: Annotated[float, Field(ge=1, le=50, allow_inf_nan=False)] | None = None,
    ) -> CallToolResult:
        return await start_action("craft", {"recipe": recipe, "count": count}, wait_seconds)

    @server.tool(description=PLACE_DESCRIPTION)
    async def place_entity(
        item: RequiredText,
        surface: RequiredText,
        x: Coordinate,
        y: Coordinate,
        direction: Literal[0, 4, 8, 12] = 0,
        underground_type: Literal["input", "output"] | None = None,
    ) -> CallToolResult:
        params = {"item": item, "surface": surface, "x": x, "y": y, "direction": direction}
        if underground_type is not None:
            if item != "underground-belt":
                return _error_response(
                    BridgeError(
                        "invalid_input", "underground_type is only valid for underground-belt."
                    )
                )
            params["underground_type"] = underground_type
        return await invoke("place_entity", params)

    @server.tool(
        description=(
            "Place 1-10 entities in order in one tick without walking. "
            "Each entry follows place_entity rules: "
            + PLACE_DESCRIPTION
            + " Walking or mining causes action_conflict for each entry; crafting is allowed. "
            "Failures do not stop later entries. Returns results with 1-based index, ok, "
            "and result or error/detail/hint using the same error codes as place_entity, "
            "plus succeeded and failed counts. The batch itself is ok: true when it ran, "
            "even if all entries failed."
        )
    )
    async def place_entities(
        items: Annotated[list[PlaceEntry], Field(min_length=1, max_length=10, strict=True)],
    ) -> CallToolResult:
        return await invoke(
            "place_entities", {"items": [entry.model_dump(exclude_none=True) for entry in items]}
        )

    @server.tool(
        description=(
            "Check placement within build reach without consuming items or building. "
            "Supports underground-belt, splitter, long-handed-inserter, and pipe-to-ground. "
            "underground_type is only valid for underground-belt: input or output "
            "(default input). direction is belt travel direction for both ends; "
            "for output it is the direction items leave. "
            "The placement area must be covered by any of the last 8 observations "
            "within 60 seconds. "
            "Items on the ground inside the footprint are picked up into the main inventory "
            "by placement when they are the only blockers and all fit. This check collects "
            "nothing: placeable=true with ground_items lists what placement would collect "
            "as {name, quality, count}, sorted by name. If they do not all fit, "
            "placeable=false, reason=blocked, blocked_reason=ground_items_inventory_full, "
            "and ground_items lists the stacks; free inventory space. "
            "Item-entity blocking entries include stack={name, quality, count}. "
            "Returns the snapped position and preview fluid_connections with port position, "
            "target_position, direction, flow, and fluidbox_index when present. "
            "For electric consumers, electric_coverage.covered reports whether the placement "
            "bounding box overlaps a pole's square supply area. For electric poles, "
            "electric_coverage.connects reports wire reach using the smaller of both poles' "
            "maximum wire distances. Coverage is included for placeable and blocked results "
            "whenever the snapped position and bounding box are computed. "
            "electric_coverage.poles lists up to 3 nearby own-force poles on the same surface, "
            "nearest first, with x, y, and electric_network_id when present; "
            "an empty list means no coverage/connection. Coverage does not guarantee power. "
            "For offshore-pump, the water_direction side must face water; pipes/boilers "
            "connect on the output_direction side. "
            "Blocked results may include blocking, tiles, blocked_reason, "
            "and valid_directions for another orientation. "
            "Each blocking entry includes a target_ref usable with mine_entity. "
            + TARGET_REF_DESCRIPTION
            + " "
            "blocked_reason is character_in_the_way when the character blocks placement "
            "(move the character away first), or placement_rules when no blocking entities, "
            "tiles, or character explain the failure. "
            "For placement_rules, nearby_positions lists up to 3 placeable centres and "
            "directions within 8 tiles of the requested position. "
            "They may be out of reach; move closer and check again with check_placement."
            " For mining drills, mining_area_resources lists mineable resources as {name, tiles} "
            "in the snapped mining area, sorted by name; an empty list also sets "
            "reason_detail to no_resources_in_mining_area."
        )
    )
    async def check_placement(
        item: RequiredText,
        surface: RequiredText,
        x: Coordinate,
        y: Coordinate,
        direction: Literal[0, 4, 8, 12] = 0,
        underground_type: Literal["input", "output"] | None = None,
    ) -> CallToolResult:
        params = {"item": item, "surface": surface, "x": x, "y": y, "direction": direction}
        if underground_type is not None:
            if item != "underground-belt":
                return _error_response(
                    BridgeError(
                        "invalid_input", "underground_type is only valid for underground-belt."
                    )
                )
            params["underground_type"] = underground_type
        return await invoke("check_placement", params)

    @server.tool(description="Rotate an owned observed entity within reach by 90 degrees.")
    async def rotate_entity(target_ref: RequiredText, reverse: bool = False) -> CallToolResult:
        return await invoke("rotate_entity", {"target_ref": target_ref, "reverse": reverse})

    @server.tool(
        description=(
            "Read accessible inventory sections of a nearby observed structure."
            + MACHINE_STATUS_DESCRIPTION
            + INVENTORY_RECIPE_DESCRIPTION
        )
    )
    async def get_entity_inventory(target_ref: RequiredText) -> CallToolResult:
        return await invoke("get_entity_inventory", {"target_ref": target_ref})

    @server.tool(
        description=(
            "Wait for an item count in a nearby observed structure instead of repeated "
            "get_entity_inventory calls while production runs. Read-only; polls about every "
            "0.5 seconds. The character must stay within reach throughout the wait; "
            "inventory read errors are returned with the same hints. "
            "count is 1-10000; timeout_seconds is 1-50 (default 20). "
            "Counts all qualities across all inventory roles, or only inventory_role if given. "
            "Accepts transfer_item role aliases: stone-furnace and assembling-machine-1 "
            "input -> source, output -> result; lab source -> input. Returned roles are canonical. "
            "Unknown roles return invalid_input with detail.valid_roles. "
            "Returns target_ref, item, count, current, reached, status (if present), stalled, "
            "timed_out, waited_seconds, and inventory_role (if given). "
            "Returns early with stalled=true, stalled_seconds, and a status hint when the "
            "count stays unchanged for 10 consecutive seconds in stopped states: "
            + ", ".join(ITEMS_STALLED_HINTS)
            + ". Working states and containers without status never stall. "
            "For full_output, take products out or add an output inserter; "
            "for ingredient shortages, supply materials; for no_fuel/no_power, supply fuel/power."
        )
    )
    async def wait_for_items(
        target_ref: RequiredText,
        item: RequiredText,
        count: Annotated[int, Field(ge=1, le=10000, strict=True)],
        inventory_role: RequiredText | None = None,
        timeout_seconds: Annotated[float, Field(ge=1, le=50, allow_inf_nan=False)] = 20,
    ) -> CallToolResult:
        started = time.monotonic()
        unchanged_since = None
        last_count = None
        try:
            while True:
                response = await call("get_entity_inventory", {"target_ref": target_ref})
                value = _success_value("get_entity_inventory", response)
                inventories = value["inventories"]
                role = ROLE_ALIASES.get(value["name"], {}).get(inventory_role, inventory_role)
                if role is not None and role not in inventories:
                    return _error_response(
                        BridgeError(
                            "invalid_input",
                            "Unknown inventory role for this entity.",
                            {
                                "entity": value["name"],
                                "inventory_role": inventory_role,
                                "valid_roles": sorted(inventories),
                                "role_aliases": ROLE_ALIASES.get(value["name"], {}),
                            },
                        )
                    )
                contents = [inventories[role]] if role is not None else inventories.values()
                current = sum(
                    stack["count"]
                    for stacks in contents
                    for stack in stacks
                    if stack["name"] == item
                )
                status = value.get("status")
                reached = current >= count
                now = time.monotonic()
                waited = now - started
                if status in ITEMS_STALLED_HINTS and not reached:
                    if unchanged_since is None or current != last_count:
                        unchanged_since = now
                else:
                    unchanged_since = None
                last_count = current
                stalled_seconds = now - unchanged_since if unchanged_since is not None else 0
                stalled = stalled_seconds >= 10
                if reached or stalled or waited >= timeout_seconds:
                    result = {
                        "target_ref": target_ref,
                        "item": item,
                        "count": count,
                        "current": current,
                        "reached": reached,
                        "stalled": stalled,
                        "timed_out": not reached and not stalled and waited >= timeout_seconds,
                        "waited_seconds": waited,
                    }
                    if status is not None:
                        result["status"] = status
                    if role is not None:
                        result["inventory_role"] = role
                    if stalled:
                        result["stalled_seconds"] = stalled_seconds
                        result["hint"] = ITEMS_STALLED_HINTS[status]
                    return _response(result)
                await asyncio.sleep(min(0.5, timeout_seconds - waited))
        except Exception as exc:
            return _error_response(exc, "get_entity_inventory")

    @server.tool(
        description=(
            "Read accessible inventory sections of 1-10 nearby observed structures "
            "in order in one tick without walking. Read-only; each target follows "
            "get_entity_inventory rules: targets must be owned, on the same surface, "
            "and within reach. " + TARGET_REF_DESCRIPTION + " "
            "Failures do not stop later entries. Returns results with 1-based index, ok, "
            "and result or error/detail/hint using the same error codes as get_entity_inventory, "
            "plus succeeded and failed counts. The batch itself is ok: true when it ran, "
            "even if all entries failed."
            + MACHINE_STATUS_DESCRIPTION
            + INVENTORY_RECIPE_DESCRIPTION
        )
    )
    async def get_entity_inventories(
        target_refs: Annotated[list[RequiredText], Field(min_length=1, max_length=10, strict=True)],
    ) -> CallToolResult:
        return await invoke("get_entity_inventories", {"target_refs": target_refs})

    @server.tool(description=TRANSFER_DESCRIPTION)
    async def transfer_item(
        target_ref: RequiredText,
        direction: Literal["to_entity", "from_entity"],
        inventory_role: RequiredText,
        item: RequiredText,
        count: Count,
    ) -> CallToolResult:
        return await invoke(
            "transfer_item",
            {
                "target_ref": target_ref,
                "direction": direction,
                "inventory_role": inventory_role,
                "item": item,
                "count": count,
            },
        )

    @server.tool(
        description=(
            "Transfer 1-10 items in order in one tick without walking. "
            "Each entry follows transfer_item rules: "
            + TRANSFER_DESCRIPTION
            + " "
            + TARGET_REF_DESCRIPTION
            + " Targets must be owned and within reach. "
            "Walking or mining causes action_conflict for each entry; crafting is allowed. "
            "Failures do not stop later entries. Returns results with 1-based index, ok, "
            "and result or error/detail/hint using the same error codes as transfer_item, "
            "plus succeeded and failed counts. The batch itself is ok: true when it ran, "
            "even if all entries failed."
        )
    )
    async def transfer_items(
        items: Annotated[list[TransferEntry], Field(min_length=1, max_length=10, strict=True)],
    ) -> CallToolResult:
        return await invoke("transfer_items", {"items": [entry.model_dump() for entry in items]})

    @server.tool(
        description=(
            "Get the status of a walking, mining, or crafting action. "
            "Running mining reports detail.gained and detail.count; "
            "detail.estimated_seconds estimates the remaining mining time when computable; "
            "running crafting reports detail.count and detail.remaining."
        )
    )
    async def get_action_status(action_id: RequiredText) -> CallToolResult:
        return await invoke("get_action_status", {"action_id": action_id})

    @server.tool(
        description=(
            "Wait for a walking, mining, or crafting action to finish. Prefer this over polling "
            "get_action_status. For move_to and craft, prefer their wait_seconds argument "
            "to save a call; use wait_for_action to continue waiting after a timeout. "
            "A timeout returns the last running status "
            "and leaves the action running. Running mining reports detail.gained and detail.count; "
            "running crafting reports detail.count and detail.remaining. "
            "When computable, mining detail.estimated_seconds is an estimate that can change "
            "if mining is interrupted or the target is depleted; use it to pick timeout_seconds. "
            "The maximum wait is 50 seconds to keep it under "
            "common 60-second client tool timeouts."
        )
    )
    async def wait_for_action(
        action_id: RequiredText,
        timeout_seconds: Annotated[float, Field(ge=1, le=50, allow_inf_nan=False)] = 20,
    ) -> CallToolResult:
        return await wait_action(action_id, timeout_seconds)

    @server.tool(description="Stop an ongoing walking, mining, or crafting action.")
    async def cancel_action(action_id: RequiredText) -> CallToolResult:
        return await invoke("cancel_action", {"action_id": action_id})

    @server.tool(
        description=(
            "After an uncertain RCON timeout, check whether the original change request executed. "
            "Do not repeat the change first."
        )
    )
    async def get_request_result(
        request_id: Annotated[str, Field(min_length=1, max_length=80)],
    ) -> CallToolResult:
        return await invoke("get_request_result", {"request_id": request_id})

    return server


def _heartbeat(bridge: FactorioBridge, stop: threading.Event) -> None:
    while not stop.wait(2):
        try:
            bridge.call("ping")
        except Exception:
            pass  # The game may not be hosted yet.


def main() -> None:
    password = os.getenv("FACTORIO_RCON_PASSWORD")
    port = int(os.getenv("FACTORIO_RCON_PORT", "27015"))
    bridge = FactorioBridge(port=port, password=password) if password else None
    stop = threading.Event()
    if bridge:
        threading.Thread(target=_heartbeat, args=(bridge, stop), daemon=True).start()
    try:
        create_server(bridge).run()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
