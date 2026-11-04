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
from pydantic import Field, ValidationError

from .bridge import BridgeError, FactorioBridge

NonNegative = Annotated[int, Field(ge=0, strict=True)]
Count = Annotated[int, Field(ge=1, le=100, strict=True)]
RequiredText = Annotated[str, Field(min_length=1)]
Coordinate = Annotated[float, Field(allow_inf_nan=False)]
ObservationRadius = Annotated[float, Field(ge=1, le=16, allow_inf_nan=False, strict=True)]
FilterNames = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=200, strict=True)]],
    Field(max_length=32, strict=True),
]

HINTS = {
    "invalid_input": (
        "Check the tool's input schema and argument values. "
        "When present, detail.errors lists each invalid field."
    ),
    "target_gone": "target_ref expires after 60 seconds. Refresh it with observe_nearby.",
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
        "If present, detail.nearby_free_position is a standable spot to try with move_to; "
        "there is no path guarantee."
    ),
    "blocked": (
        "Check valid_directions for another orientation, mine or recover blocking entities, "
        "or find another position with check_placement. "
        "If reason is character_in_the_way, move the character away first. "
        "For placement_rules, nearby_positions lists up to 3 placeable centres and directions "
        "within 8 tiles of the requested position. They may be out of reach; "
        "move closer and check again with check_placement."
    ),
    "invalid_target": (
        "Check the entity, valid_roles, and role_aliases in detail. "
        "Furnaces and assemblers accept input -> source and output -> result; "
        "labs accept source -> input."
    ),
    "out_of_range": "Move closer to the target, then observe_nearby again.",
    "missing_materials": (
        "Gather the missing items before retrying. "
        "For crafting, detail.missing_raw lists raw items such as plates that are short "
        "after expanding hand-craftable intermediates and using held materials first. "
        "For transfers, check detail.contents for items in the source inventory."
    ),
}


def _normalize_result(operation: str, result: dict[str, Any]) -> dict[str, Any]:
    if "role_aliases" in result and not isinstance(result["role_aliases"], dict):
        result["role_aliases"] = {}
    array_fields = {
        "observe_nearby": ("entities", "blocked_tiles", "resources"),
        "get_explored_map": ("resources", "water", "trees", "rocks"),
        "get_inventory": ("items",),
        "get_agent_status": ("inventory",),
        "list_craftable_recipes": ("recipes",),
        "get_research_status": ("researched", "queue", "available"),
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
            for field in ("belt_items", "fluids", "inventory_roles"):
                if field in entity and not isinstance(entity[field], list):
                    entity[field] = []
    if operation == "check_placement":
        for field in ("blocking", "tiles"):
            if field in result and not isinstance(result[field], list):
                result[field] = []
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


def _error_response(exc: Exception) -> CallToolResult:
    code = exc.code if isinstance(exc, BridgeError) else "internal_error"
    detail = exc.detail if isinstance(exc, BridgeError) else None
    value: dict[str, Any] = {"ok": False, "error": code, "message": str(exc)}
    if isinstance(detail, dict):
        if detail.get("request_id") is not None:
            value["request_id"] = detail["request_id"]
        public_detail = {key: item for key, item in detail.items() if key != "request_id"}
        if "role_aliases" in public_detail and not isinstance(public_detail["role_aliases"], dict):
            public_detail["role_aliases"] = {}
        # Factorio serializes empty Lua arrays as objects.
        for field in (
            "blocking",
            "tiles",
            "missing",
            "missing_raw",
            "valid_roles",
            "supported_entities",
            "contents",
        ):
            if field in public_detail and not isinstance(public_detail[field], list):
                public_detail[field] = []
        if public_detail:
            value["detail"] = public_detail
    if code in HINTS:
        value["hint"] = HINTS[code]
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
                )
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
            return _error_response(exc)

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
            "reach gives build (placing), interact (transferring, rotating, and setting recipes), "
            "and mine_resource (resource/tree/rock mining) reach in tiles. "
            "Resources, trees, and rocks are judged to their collision edge, so they may be "
            "reachable at a slightly larger distance than mine_resource. "
            "With include_resources = false, individual resource entities are omitted but the "
            "resources summary still returns per-resource tile count, total amount, "
            "and bounding area. "
            "Target refs and placement observations expire after 60 seconds. "
            "Supported structures in your force include sorted inventory_roles. "
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
            "independent of filters and paging."
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
        description="Read technologies researched by the AI character’s force and current research."
    )
    async def get_research_status() -> CallToolResult:
        return await invoke("get_research_status")

    @server.tool(
        description=(
            "Wait for a technology to finish research. Returns early if it is not queued. "
            "Prefer this over polling get_research_status. The maximum wait is 50 seconds "
            "to keep it under common 60-second client tool timeouts."
        )
    )
    async def wait_for_research(
        technology: RequiredText,
        timeout_seconds: Annotated[float, Field(ge=1, le=50, allow_inf_nan=False)] = 20,
    ) -> CallToolResult:
        started = time.monotonic()
        try:
            while True:
                response = await call("get_research_status")
                value = _success_value("get_research_status", response)
                researched = technology in value["researched"]
                current = value.get("current") == technology
                researching = current or technology in value["queue"]
                waited = time.monotonic() - started
                if researched or not researching or waited >= timeout_seconds:
                    return _response(
                        {
                            "technology": technology,
                            "researched": researched,
                            "researching": researching,
                            "progress": value.get("progress") if current else None,
                            "timed_out": not researched
                            and researching
                            and waited >= timeout_seconds,
                            "waited_seconds": waited,
                        }
                    )
                await asyncio.sleep(min(0.25, timeout_seconds - waited))
        except Exception as exc:
            return _error_response(exc)

    @server.tool(
        description="Read a force recipe, its materials, products, and hand crafting availability."
    )
    async def get_recipe_info(recipe: RequiredText) -> CallToolResult:
        return await invoke("get_recipe_info", {"recipe": recipe})

    @server.tool(
        description="Set an enabled recipe on an empty owned assembling machine within reach."
    )
    async def set_recipe(target_ref: RequiredText, recipe: RequiredText) -> CallToolResult:
        return await invoke("set_recipe", {"target_ref": target_ref, "recipe": recipe})

    @server.tool(
        description="Queue an available technology for lab research consuming science packs."
    )
    async def start_research(technology: RequiredText) -> CallToolResult:
        return await invoke("start_research", {"technology": technology})

    @server.tool(
        description=(
            "Walk to a position on the current surface. Arrival is within 0.5 tiles. "
            "The goal must be within 128 tiles in a straight line from the character. "
            "For farther goals, move via intermediate points. "
            "If the goal is blocked, succeeds within 1.5 tiles with detail.goal_blocked = true "
            "and detail.distance. Blocked or timeout failures include detail.distance and "
            "detail.goal. path_not_found includes goal details and may include goal_tiles, "
            "goal_entities, and nearby_free_position (a standable spot with no path guarantee). "
            "Crafting runs alongside walking. Only one walk/mine at a time. "
            "Returns an action ID for polling."
        )
    )
    async def move_to(surface: RequiredText, x: Coordinate, y: Coordinate) -> CallToolResult:
        return await invoke("move_to", {"surface": surface, "x": x, "y": y})

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
            "Poll the returned action ID. On missing_materials, detail.missing_raw lists "
            "raw items such as plates that are short after expanding hand-craftable "
            "intermediates and using held materials first."
        )
    )
    async def craft(recipe: RequiredText, count: Count) -> CallToolResult:
        return await invoke("craft", {"recipe": recipe, "count": count})

    @server.tool(
        description=(
            "Place a supported entity within build reach, consuming one real inventory item."
            " Supports underground-belt, splitter, long-handed-inserter, and pipe-to-ground."
            " underground_type is only valid for underground-belt: input or output"
            " (default input). direction is belt travel direction for both ends;"
            " for output it is the direction items leave."
            " The placement area must be covered by any of the last 8 observations"
            " within 60 seconds."
            " Returns the snapped position and actual fluid_connections with port position,"
            " target_position, direction, flow, fluidbox_index, and connected when present."
            " For offshore-pump, the water_direction side must face water; pipes/boilers"
            " connect on the output_direction side (see check_placement or observe_nearby)."
            " Blocked errors may include valid_directions for another orientation."
            " Each detail.blocking entry includes a target_ref usable with mine_entity"
            " for 60 seconds, subject to reach and force checks."
            " detail.reason is character_in_the_way when the character blocks placement"
            " (move the character away first), or placement_rules when no blocking entities,"
            " tiles, or character explain the failure."
            " For placement_rules, detail.nearby_positions lists up to 3 placeable centres"
            " and directions within 8 tiles of the requested position."
            " They may be out of reach; move closer and check again with check_placement."
        )
    )
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
            "Check placement within build reach without consuming items or building. "
            "Supports underground-belt, splitter, long-handed-inserter, and pipe-to-ground. "
            "underground_type is only valid for underground-belt: input or output "
            "(default input). direction is belt travel direction for both ends; "
            "for output it is the direction items leave. "
            "The placement area must be covered by any of the last 8 observations "
            "within 60 seconds. "
            "Returns the snapped position and preview fluid_connections with port position, "
            "target_position, direction, flow, and fluidbox_index when present. "
            "For offshore-pump, the water_direction side must face water; pipes/boilers "
            "connect on the output_direction side. "
            "Blocked results may include blocking, tiles, blocked_reason, "
            "and valid_directions for another orientation. "
            "Each blocking entry includes a target_ref usable with mine_entity for 60 seconds, "
            "subject to reach and force checks. "
            "blocked_reason is character_in_the_way when the character blocks placement "
            "(move the character away first), or placement_rules when no blocking entities, "
            "tiles, or character explain the failure. "
            "For placement_rules, nearby_positions lists up to 3 placeable centres and "
            "directions within 8 tiles of the requested position. "
            "They may be out of reach; move closer and check again with check_placement."
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

    @server.tool(description="Read accessible inventory sections of a nearby observed structure.")
    async def get_entity_inventory(target_ref: RequiredText) -> CallToolResult:
        return await invoke("get_entity_inventory", {"target_ref": target_ref})

    @server.tool(
        description=(
            "Move a real item between the AI character and a nearby observed structure. "
            "count is 1-100. "
            "Choose inventory_role from the structure's inventory_roles in observe_nearby. "
            "Role aliases: stone-furnace and assembling-machine-1 accept input -> source "
            "and output -> result; lab accepts source -> input. Returned roles stay canonical. "
            "missing_materials detail includes item, requested, available (0), and contents "
            "of the source inventory (up to 10 items sorted by name, omitted when empty). "
            "inventory_role is included when taking items from the structure."
        )
    )
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
            "get_action_status. A timeout returns the last running status "
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
        started = time.monotonic()
        try:
            while True:
                response = await call("get_action_status", {"action_id": action_id})
                value = _success_value("get_action_status", response)
                waited = time.monotonic() - started
                running = value.get("status") == "running"
                if not running or waited >= timeout_seconds:
                    return _response({**value, "timed_out": running, "waited_seconds": waited})
                await asyncio.sleep(min(0.25, timeout_seconds - waited))
        except Exception as exc:
            return _error_response(exc)

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
