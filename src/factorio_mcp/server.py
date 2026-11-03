"""STDIO MCP tools for one Factorio AI character."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import Field

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
    "target_gone": "target_ref expires after 60 seconds. Refresh it with observe_nearby.",
    "not_visible": "Observe the placement area within 60 seconds before placing.",
    "action_conflict": "Wait with wait_for_action, or stop the running action with cancel_action.",
    "blocked": "Mine or recover blocking entities, or find another position with check_placement.",
    "invalid_target": "Check the entity and valid_roles in detail, then choose a supported target.",
    "out_of_range": "Move closer to the target, then observe_nearby again.",
    "missing_materials": "Gather the missing items before retrying.",
}


def _normalize_result(operation: str, result: dict[str, Any]) -> dict[str, Any]:
    array_fields = {
        "observe_nearby": ("entities", "blocked_tiles", "resources"),
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
        # Factorio serializes empty Lua arrays as objects.
        for field in ("blocking", "tiles", "missing", "valid_roles", "supported_entities"):
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


def create_server(bridge: FactorioBridge | None) -> MCPServer:
    server = MCPServer("factorio-ai", version="0.1.0")

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
        description="Get the AI character position, health, inventory, and current action."
    )
    async def get_agent_status() -> CallToolResult:
        return await invoke("get_agent_status")

    @server.tool(
        description=(
            "Observe nearby entities and blocked terrain tiles within 16 tiles. "
            "Target refs and placement observations expire after 60 seconds. "
            "Supported structures in your force include sorted inventory_roles. "
            "Follow offsets for additional pages."
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
        description="Walk to a position on the current surface. Returns an action ID for polling."
    )
    async def move_to(surface: RequiredText, x: Coordinate, y: Coordinate) -> CallToolResult:
        return await invoke("move_to", {"surface": surface, "x": x, "y": y})

    @server.tool(
        description=(
            "Mine an observed entity within reach over normal game time. Returns an action ID."
        )
    )
    async def mine_entity(target_ref: RequiredText, count: Count = 1) -> CallToolResult:
        return await invoke("mine_entity", {"target_ref": target_ref, "count": count})

    @server.tool(
        description=(
            "Start normal hand crafting using the AI character’s own materials. "
            "Poll the returned action ID."
        )
    )
    async def craft(recipe: RequiredText, count: Count) -> CallToolResult:
        return await invoke("craft", {"recipe": recipe, "count": count})

    @server.tool(
        description=(
            "Place a supported entity within build reach, consuming one real inventory item."
            " Observe the placement area within 60 seconds before placing."
        )
    )
    async def place_entity(
        item: RequiredText,
        surface: RequiredText,
        x: Coordinate,
        y: Coordinate,
        direction: Literal[0, 4, 8, 12] = 0,
    ) -> CallToolResult:
        return await invoke(
            "place_entity",
            {"item": item, "surface": surface, "x": x, "y": y, "direction": direction},
        )

    @server.tool(
        description=(
            "Check placement within build reach without consuming items or building. "
            "Requires an observation of the placement area within 60 seconds."
        )
    )
    async def check_placement(
        item: RequiredText,
        surface: RequiredText,
        x: Coordinate,
        y: Coordinate,
        direction: Literal[0, 4, 8, 12] = 0,
    ) -> CallToolResult:
        return await invoke(
            "check_placement",
            {"item": item, "surface": surface, "x": x, "y": y, "direction": direction},
        )

    @server.tool(description="Rotate an owned observed entity within reach by 90 degrees.")
    async def rotate_entity(target_ref: RequiredText, reverse: bool = False) -> CallToolResult:
        return await invoke("rotate_entity", {"target_ref": target_ref, "reverse": reverse})

    @server.tool(description="Read accessible inventory sections of a nearby observed structure.")
    async def get_entity_inventory(target_ref: RequiredText) -> CallToolResult:
        return await invoke("get_entity_inventory", {"target_ref": target_ref})

    @server.tool(
        description=(
            "Move a real item between the AI character and a nearby observed structure. "
            "Choose inventory_role from the structure's inventory_roles in observe_nearby."
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

    @server.tool(description="Get the status of a walking, mining, or crafting action.")
    async def get_action_status(action_id: RequiredText) -> CallToolResult:
        return await invoke("get_action_status", {"action_id": action_id})

    @server.tool(
        description=(
            "Wait for a walking, mining, or crafting action to finish. Prefer this over polling "
            "get_action_status. A timeout returns the last running status "
            "and leaves the action running. The maximum wait is 50 seconds to keep it under "
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
