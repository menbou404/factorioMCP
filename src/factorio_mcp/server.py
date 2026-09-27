"""STDIO MCP tools for one Factorio AI character."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from .bridge import BridgeError, FactorioBridge

NonNegative = Annotated[int, Field(ge=0, strict=True)]
Count = Annotated[int, Field(ge=1, le=100, strict=True)]
RequiredText = Annotated[str, Field(min_length=1)]
Coordinate = Annotated[float, Field(allow_inf_nan=False)]


def _normalize_result(operation: str, result: dict[str, Any]) -> dict[str, Any]:
    array_fields = {
        "observe_nearby": ("entities", "blocked_tiles"),
        "get_inventory": ("items",),
        "get_agent_status": ("inventory",),
        "list_craftable_recipes": ("recipes",),
        "get_research_status": ("researched",),
    }
    for field in array_fields.get(operation, ()):
        if not isinstance(result.get(field), list):
            result[field] = []
    if operation == "get_entity_inventory" and isinstance(result.get("inventories"), dict):
        for role, items in result["inventories"].items():
            if not isinstance(items, list):
                result["inventories"][role] = []
    return result


def _response(value: dict[str, Any], *, error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(text=json.dumps(value, ensure_ascii=False))], isError=error
    )


def create_server(bridge: FactorioBridge | None) -> MCPServer:
    server = MCPServer("factorio-ai", version="0.1.0")

    async def invoke(operation: str, params: dict[str, Any] | None = None) -> CallToolResult:
        if bridge is None:
            return _response({
                "ok": False, "error": "not_configured",
                "message": "Set FACTORIO_RCON_PASSWORD before starting the MCP server.",
            }, error=True)
        try:
            response = await asyncio.to_thread(bridge.call, operation, params)
            result = response.get("result") or {}
            return _response({
                "ok": True, "tick": response.get("tick"),
                **_normalize_result(operation, result),
            })
        except Exception as exc:
            code = exc.code if isinstance(exc, BridgeError) else "internal_error"
            detail = exc.detail if isinstance(exc, BridgeError) else None
            request_id = detail.get("request_id") if isinstance(detail, dict) else None
            value: dict[str, Any] = {"ok": False, "error": code, "message": str(exc)}
            if request_id is not None:
                value["request_id"] = request_id
            return _response(value, error=True)

    @server.tool(description="Check the connection to the Factorio AI Mod.")
    async def factorio_ping() -> CallToolResult:
        return await invoke("ping")

    @server.tool(description="Get the AI character position, health, inventory, and current action.")
    async def get_agent_status() -> CallToolResult:
        return await invoke("get_agent_status")

    @server.tool(description="Observe nearby entities and blocked terrain tiles within 16 tiles. Follow offsets for additional pages.")
    async def observe_nearby(entity_offset: NonNegative = 0, tile_offset: NonNegative = 0) -> CallToolResult:
        return await invoke("observe_nearby", {"entity_offset": entity_offset, "tile_offset": tile_offset})

    @server.tool(description="Read the AI character’s actual in-game inventory.")
    async def get_inventory() -> CallToolResult:
        return await invoke("get_inventory")

    @server.tool(description="Page through unlocked hand crafting recipes, ingredients, and current craftable counts.")
    async def list_craftable_recipes(offset: NonNegative = 0, limit: Annotated[int, Field(ge=1, le=100, strict=True)] = 50) -> CallToolResult:
        return await invoke("list_craftable_recipes", {"offset": offset, "limit": limit})

    @server.tool(description="Read technologies researched by the AI character’s force and current research.")
    async def get_research_status() -> CallToolResult:
        return await invoke("get_research_status")

    @server.tool(description="Walk to a position on the current surface. Returns an action ID for polling.")
    async def move_to(surface: RequiredText, x: Coordinate, y: Coordinate) -> CallToolResult:
        return await invoke("move_to", {"surface": surface, "x": x, "y": y})

    @server.tool(description="Mine an observed entity within reach over normal game time. Returns an action ID.")
    async def mine_entity(target_ref: RequiredText, count: Count = 1) -> CallToolResult:
        return await invoke("mine_entity", {"target_ref": target_ref, "count": count})

    @server.tool(description="Start normal hand crafting using the AI character’s own materials. Poll the returned action ID.")
    async def craft(recipe: RequiredText, count: Count) -> CallToolResult:
        return await invoke("craft", {"recipe": recipe, "count": count})

    @server.tool(description="Place a supported entity within build reach, consuming one real inventory item.")
    async def place_entity(item: RequiredText, surface: RequiredText, x: Coordinate, y: Coordinate, direction: Literal[0, 4, 8, 12] = 0) -> CallToolResult:
        return await invoke("place_entity", {"item": item, "surface": surface, "x": x, "y": y, "direction": direction})

    @server.tool(description="Read accessible inventory sections of a nearby observed structure.")
    async def get_entity_inventory(target_ref: RequiredText) -> CallToolResult:
        return await invoke("get_entity_inventory", {"target_ref": target_ref})

    @server.tool(description="Move a real item between the AI character and a nearby observed structure.")
    async def transfer_item(target_ref: RequiredText, direction: Literal["to_entity", "from_entity"], inventory_role: RequiredText, item: RequiredText, count: Count) -> CallToolResult:
        return await invoke("transfer_item", {"target_ref": target_ref, "direction": direction, "inventory_role": inventory_role, "item": item, "count": count})

    @server.tool(description="Get the status of a walking, mining, or crafting action.")
    async def get_action_status(action_id: RequiredText) -> CallToolResult:
        return await invoke("get_action_status", {"action_id": action_id})

    @server.tool(description="Stop an ongoing walking, mining, or crafting action.")
    async def cancel_action(action_id: RequiredText) -> CallToolResult:
        return await invoke("cancel_action", {"action_id": action_id})

    @server.tool(description="After an uncertain RCON timeout, check whether the original change request executed. Do not repeat the change first.")
    async def get_request_result(request_id: Annotated[str, Field(min_length=1, max_length=80)]) -> CallToolResult:
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
