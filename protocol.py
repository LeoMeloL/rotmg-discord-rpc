"""RotMG wire protocol: RC4 stream cipher, framing and the few packets we read.

Wire format (per TCP direction): [int32 BE total length][uint8 packet id][body].
The body is RC4-encrypted with a static key; the cipher state runs across all
packets of one TCP connection and restarts on every new connection (every map
change opens a new connection).

Packet ids / field layouts mirror client/data/packet-definitions.json in the
Realm Engine repo. If the game patches and ids move, update PACKET_IDS.
"""
from __future__ import annotations

import logging
import struct

log = logging.getLogger("protocol")

SERVER_KEY = bytes.fromhex("c91d9eec420160730d825604e0")  # server -> client
CLIENT_KEY = bytes.fromhex("5a4d2016bc16dc64883194ffd9")  # client -> server

PACKET_IDS = {
    "FAILURE": 0,
    "NEWTICK": 10,
    "UPDATE": 42,
    "DEATH": 46,
    "QUESTOBJECTID": 82,
    "MAPINFO": 92,
    "CREATESUCCESS": 101,
}
ID_TO_NAME = {v: k for k, v in PACKET_IDS.items()}

# StatData ids whose value is a string instead of a compressed int.
# 78 became a string in game build 7.1 (seen live as "Name|Name").
STRING_STATS = {6, 31, 38, 54, 62, 71, 72, 78, 80, 82, 115, 121, 127, 128, 147, 155}

STAT_MAX_HP = 0
STAT_HP = 1
STAT_LEVEL = 7
STAT_NAME = 31
STAT_CURRENT_FAME = 39
STAT_CHARACTER_FAME = 57


class RC4:
    def __init__(self, key: bytes):
        s = list(range(256))
        j = 0
        for i in range(256):
            j = (j + s[i] + key[i % len(key)]) & 0xFF
            s[i], s[j] = s[j], s[i]
        self.s = s
        self.x = 0
        self.y = 0

    def process(self, data: bytes) -> bytes:
        s, x, y = self.s, self.x, self.y
        out = bytearray(len(data))
        for n, b in enumerate(data):
            x = (x + 1) & 0xFF
            sx = s[x]
            y = (y + sx) & 0xFF
            sy = s[y]
            s[x] = sy
            s[y] = sx
            out[n] = b ^ s[(sx + sy) & 0xFF]
        self.x, self.y = x, y
        return bytes(out)


class Reader:
    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes):
        self.buf = buf
        self.pos = 0

    def _unpack(self, fmt: str, size: int):
        v = struct.unpack_from(fmt, self.buf, self.pos)[0]
        self.pos += size
        return v

    def u8(self) -> int:
        return self._unpack(">B", 1)

    def i16(self) -> int:
        return self._unpack(">h", 2)

    def u16(self) -> int:
        return self._unpack(">H", 2)

    def i32(self) -> int:
        return self._unpack(">i", 4)

    def f32(self) -> float:
        return self._unpack(">f", 4)

    def string(self) -> str:
        n = self.i16()
        if n < 0 or self.pos + n > len(self.buf):
            raise ValueError(f"bad string length {n}")
        s = self.buf[self.pos:self.pos + n].decode("utf-8", "replace")
        self.pos += n
        return s

    def cint(self) -> int:
        b = self.u8()
        neg = b & 0x40
        result = b & 0x3F
        shift = 6
        while b & 0x80:
            b = self.u8()
            result |= (b & 0x7F) << shift
            shift += 7
        return -result if neg else result


def read_status(r: Reader) -> tuple[int, float, float, dict[int, int | str]]:
    oid = r.cint()
    x = r.f32()
    y = r.f32()
    stats: dict[int, int | str] = {}
    for _ in range(r.cint()):
        sid = r.u8()
        stats[sid] = r.string() if sid in STRING_STATS else r.cint()
        r.cint()  # stackCount
    return oid, x, y, stats


def parse_mapinfo(body: bytes) -> dict:
    r = Reader(body)
    width = r.i32()
    height = r.i32()
    name = r.string()
    display_name = r.string()
    realm_name = r.string()
    r.i32(); r.i32()      # fp, background
    r.f32()               # difficulty
    r.u8(); r.u8(); r.u8()  # allowPlayerTeleport, noSave, showDisplays
    max_players = r.i16()
    return {"width": width, "height": height, "name": name, "display_name": display_name,
            "realm_name": realm_name, "max_players": max_players}


def parse_update(body: bytes) -> tuple[list, list[int]]:
    """Returns ([(objectType, oid, x, y, stats)], drops).

    If a game patch changes a stat's type, parsing stops at that object but
    the objects decoded before it are still returned.
    """
    r = Reader(body)
    r.f32(); r.f32()  # position
    r.u8()            # levelType
    tiles = r.cint()
    r.pos += tiles * 6  # int16 x, int16 y, uint16 type
    new_objs = []
    try:
        for _ in range(r.cint()):
            obj_type = r.u16()
            oid, x, y, stats = read_status(r)
            new_objs.append((obj_type, oid, x, y, stats))
        drops = [r.cint() for _ in range(r.cint())]
    except (ValueError, struct.error) as e:
        log.warning("UPDATE partially decoded (%d objects): %s", len(new_objs), e)
        return new_objs, []
    return new_objs, drops


def parse_newtick(body: bytes) -> list:
    r = Reader(body)
    r.i32(); r.i32()  # tickId, tickTime
    r.pos += 4 + 2    # serverRealTimeMs (u32), serverLastRttMs (u16)
    statuses = []
    try:
        for _ in range(r.i16()):
            statuses.append(read_status(r))
    except (ValueError, struct.error) as e:
        log.warning("NEWTICK partially decoded (%d statuses): %s", len(statuses), e)
    return statuses


def parse_int32(body: bytes) -> int:
    return struct.unpack_from(">i", body, 0)[0]


class StreamDecoder:
    """Reassembled server->client byte stream -> decrypted (id, body) packets."""

    MAX_PACKET = 8 * 1024 * 1024

    def __init__(self):
        self.rc4 = RC4(SERVER_KEY)
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf += data
        while len(self.buf) >= 5:
            length = struct.unpack_from(">i", self.buf, 0)[0]
            if length < 5 or length > self.MAX_PACKET:
                raise ValueError(f"desync: packet length {length}")
            if len(self.buf) < length:
                return
            pid = self.buf[4]
            body = self.rc4.process(bytes(self.buf[5:length]))
            del self.buf[:length]
            yield pid, body
