"""Serialized, loopback-only RCON transport for the Factorio Mod protocol."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from rcon.source import Client


class BridgeError(Exception):
    def __init__(self, code: str, message: str | None = None, detail: Any = None):
        super().__init__(message or code)
        self.code = code
        self.detail = detail


def parse_bridge_response(raw: str, request_id: str) -> dict[str, Any]:
    for line in str(raw).splitlines():
        try:
            response = json.loads(line.strip())
        except (ValueError, TypeError):
            continue
        if not isinstance(response, dict):
            continue
        if response.get("request_id") != request_id or response.get("protocol_version") != 1:
            continue
        if not response.get("ok"):
            raise BridgeError(
                response.get("error") or "game_error",
                response.get("error"),
                response.get("result"),
            )
        return response
    raise BridgeError("mod_unavailable", "Factorio Mod did not return a matching response")


def _send_rcon(host: str, port: int, password: str, command: str) -> str:
    with Client(host, port, passwd=password, timeout=5) as client:
        return client.run(command)


class FactorioBridge:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 27015,
        password: str,
        send: Callable[[str, int, str, str], str] = _send_rcon,
    ) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise BridgeError("invalid_config", "Only loopback RCON hosts are supported")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise BridgeError("invalid_config", "RCON port must be in 1..65535")
        if not password:
            raise BridgeError("invalid_config", "FACTORIO_RCON_PASSWORD is required")
        self.host = host
        self.port = port
        self.password = password
        self.send = send
        self._lock = threading.Lock()

    def call(self, operation: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = str(uuid4())
        request = {
            "protocol_version": 1,
            "request_id": request_id,
            "agent_id": "agent-1",
            "operation": operation,
            "params": params or {},
        }
        command = "/ai_mcp " + json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        # The Mod expects one request at a time for the single AI character.
        with self._lock:
            for attempt in range(2):
                try:
                    return parse_bridge_response(
                        self.send(self.host, self.port, self.password, command), request_id
                    )
                except BridgeError:
                    raise
                except Exception as error:
                    if attempt == 1:
                        raise BridgeError(
                            "not_connected", "Could not reach Factorio RCON",
                            {"request_id": request_id},
                        ) from error
        raise AssertionError("Unreachable")
