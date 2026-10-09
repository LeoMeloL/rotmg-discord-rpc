"""Minimal Discord RPC client over the local named pipe (no dependencies)."""
from __future__ import annotations

import json
import logging
import os
import struct
import uuid

log = logging.getLogger("discord")

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2


class DiscordIPC:
    def __init__(self, client_id: str):
        self.client_id = client_id
        self.pipe = None

    @property
    def connected(self) -> bool:
        return self.pipe is not None

    def connect(self) -> bool:
        for i in range(10):
            try:
                pipe = open(rf"\\.\pipe\discord-ipc-{i}", "r+b", buffering=0)
            except OSError:
                continue
            self.pipe = pipe
            try:
                self._send(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
                op, data = self._recv()
            except OSError:
                self.close()
                continue
            if op == OP_FRAME and data.get("evt") == "READY":
                user = (data.get("data") or {}).get("user") or {}
                log.info("Connected to Discord as %s", user.get("username", "?"))
                return True
            log.warning("Discord handshake rejected: %s", data)
            self.close()
            return False
        return False

    def close(self):
        if self.pipe is not None:
            try:
                self.pipe.close()
            except OSError:
                pass
        self.pipe = None

    def _send(self, op: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.pipe.write(struct.pack("<ii", op, len(data)) + data)

    def _read_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.pipe.read(n - len(buf))
            if not chunk:
                raise OSError("Discord pipe closed")
            buf += chunk
        return buf

    def _recv(self) -> tuple[int, dict]:
        op, length = struct.unpack("<ii", self._read_exact(8))
        return op, json.loads(self._read_exact(length).decode("utf-8"))

    def set_activity(self, activity: dict | None) -> bool:
        """Sets (or clears, with None) the presence. Returns False if the pipe broke."""
        if self.pipe is None:
            return False
        payload = {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(uuid.uuid4()),
        }
        try:
            self._send(OP_FRAME, payload)
            op, data = self._recv()
        except OSError as e:
            log.warning("Lost connection to Discord: %s", e)
            self.close()
            return False
        if op == OP_CLOSE:
            log.warning("Discord closed the connection: %s", data)
            self.close()
            return False
        if data.get("evt") == "ERROR":
            log.warning("Discord rejected the activity: %s", data.get("data"))
        return True
