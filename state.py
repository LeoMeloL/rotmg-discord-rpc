"""Game state rebuilt from decoded server packets."""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass

import protocol as P
from gamedata import FLAG_QUEST, GameData, prettify_name

log = logging.getLogger("state")

# Friendlier names for maps whose MAPINFO name is terse or a token.
MAP_ALIASES = {
    "rotmg": "Realm",
    "nexus": "Nexus",
    "vault": "Vault",
    "petyard": "Pet Yard",
    "guildhall": "Guild Hall",
    "daily quest room": "Daily Quest Room",
}


def area_name(name: str, display_name: str) -> str:
    raw = prettify_name(display_name) or prettify_name(name) or name
    return MAP_ALIASES.get(raw.lower(), raw)


@dataclass
class Snapshot:
    in_game: bool
    area: str = ""
    map_name: str = ""  # raw MAPINFO name, second key for icon lookup
    realm: str = ""
    class_name: str = ""
    level: int = 0
    fame: int = 0
    boss: str = ""
    boss_hp_pct: int = -1
    boss_type: int = 0
    class_type: int = 0
    area_since: float = 0.0


class GameState:
    def __init__(self, gamedata: GameData, boss_min_hp: int = 5000, boss_radius: float = 15.0):
        self.gd = gamedata
        self.boss_min_hp = boss_min_hp
        self.boss_radius = boss_radius
        self.lock = threading.Lock()
        self.flow = None          # connection whose packets we trust (latest MAPINFO)
        self.my_oid = -1
        self.class_type = 0
        self.level = 0
        self.fame = 0             # stat 57: fame of the current character
        self.account_fame = 0     # stat 39
        self.pos = (0.0, 0.0)
        self.area = ""
        self.map_name = ""
        self.realm = ""
        self.area_since = 0.0
        self.quest_oid = -1
        # oid -> [objectType, x, y, hp, maxHp]
        self.enemies: dict[int, list] = {}
        self.closed_at = 0.0
        self.last_boss = ("", -1, 0)
        self.last_boss_seen = 0.0
        self.packets_seen = 0

    # ---- packet handlers (sniffer thread) ----

    def on_packet(self, flow, pid: int, body: bytes):
        self.packets_seen += 1
        name = P.ID_TO_NAME.get(pid)
        if name is None:
            return
        with self.lock:
            if name == "MAPINFO":
                self._on_mapinfo(flow, P.parse_mapinfo(body))
                return
            if flow != self.flow:
                return
            if name == "CREATESUCCESS":
                self.my_oid = P.parse_int32(body)
                log.debug("CREATESUCCESS objectId=%d", self.my_oid)
            elif name == "UPDATE":
                self._on_update(*P.parse_update(body))
            elif name == "NEWTICK":
                self._on_newtick(P.parse_newtick(body))
            elif name == "QUESTOBJECTID":
                self.quest_oid = P.parse_int32(body)

    def on_flow_closed(self, flow):
        with self.lock:
            if flow == self.flow:
                self.flow = None
                self.closed_at = time.time()

    def _on_mapinfo(self, flow, m: dict):
        prev_area = self.area
        self.flow = flow
        self.area = area_name(m["name"], m["display_name"])
        self.map_name = m["name"]
        self.realm = prettify_name(m["realm_name"])
        if self.area != prev_area or not self.area_since:
            self.area_since = time.time()
        self.my_oid = -1
        self.quest_oid = -1
        self.enemies.clear()
        log.info("Area: %s (name=%r display=%r realm=%r)", self.area, m["name"],
                 m["display_name"], m["realm_name"])

    def _apply_self_stats(self, stats: dict):
        if P.STAT_LEVEL in stats:
            self.level = int(stats[P.STAT_LEVEL])
        if P.STAT_CHARACTER_FAME in stats:
            self.fame = int(stats[P.STAT_CHARACTER_FAME])
        if P.STAT_CURRENT_FAME in stats:
            self.account_fame = int(stats[P.STAT_CURRENT_FAME])

    def _on_update(self, new_objs, drops):
        for obj_type, oid, x, y, stats in new_objs:
            if oid == self.my_oid:
                if obj_type != self.class_type:
                    log.info("Class: %s (%d)", self.gd.class_name(obj_type) or "?", obj_type)
                self.class_type = obj_type
                self.pos = (x, y)
                self._apply_self_stats(stats)
                continue
            if self.gd.enemy(obj_type) is not None:
                self.enemies[oid] = [obj_type, x, y,
                                     int(stats.get(P.STAT_HP, 0) or 0),
                                     int(stats.get(P.STAT_MAX_HP, 0) or 0)]
        for oid in drops:
            self.enemies.pop(oid, None)

    def _on_newtick(self, statuses):
        for oid, x, y, stats in statuses:
            if oid == self.my_oid:
                self.pos = (x, y)
                self._apply_self_stats(stats)
                continue
            e = self.enemies.get(oid)
            if e is None:
                continue
            e[1], e[2] = x, y
            if P.STAT_HP in stats:
                e[3] = int(stats[P.STAT_HP])
            if P.STAT_MAX_HP in stats:
                e[4] = int(stats[P.STAT_MAX_HP])

    # ---- queries (presence thread) ----

    def _current_boss(self) -> tuple[str, int, int]:
        """(name, hp %, objectType) of the boss being fought, or ("", -1, 0)."""
        best = None
        px, py = self.pos
        for oid, (obj_type, x, y, hp, max_hp) in self.enemies.items():
            name, xml_hp, flags = self.gd.enemy(obj_type)
            if not flags & FLAG_QUEST or max(xml_hp, max_hp) < self.boss_min_hp:
                continue
            dist = math.hypot(x - px, y - py)
            if dist > self.boss_radius:
                continue
            # Prefer the server's quest target, then the closest boss.
            rank = (oid != self.quest_oid, dist)
            if best is None or rank < best[0]:
                pct = round(100 * hp / max_hp) if max_hp > 0 else -1
                best = (rank, name, pct, obj_type)
        if best is None:
            return "", -1, 0
        return best[1], best[2], best[3]

    def snapshot(self, reconnect_grace: float = 8.0) -> Snapshot:
        with self.lock:
            connected = self.flow is not None or (time.time() - self.closed_at) < reconnect_grace
            if not connected or not self.area:
                return Snapshot(in_game=False)
            boss, pct, boss_type = self._current_boss()
            now = time.time()
            if boss:
                self.last_boss, self.last_boss_seen = (boss, pct, boss_type), now
            elif self.last_boss[0] and now - self.last_boss_seen < 5 and self.flow is not None:
                boss, pct, boss_type = self.last_boss  # smooth over brief out-of-range moments
            return Snapshot(
                in_game=True,
                area=self.area,
                map_name=self.map_name,
                realm=self.realm,
                class_name=self.gd.class_name(self.class_type),
                level=self.level,
                fame=self.fame,
                boss=boss,
                boss_hp_pct=pct,
                boss_type=boss_type,
                class_type=self.class_type,
                area_since=self.area_since,
            )

    def debug_line(self) -> str:
        with self.lock:
            return (f"pkts={self.packets_seen} area={self.area!r} realm={self.realm!r} oid={self.my_oid} "
                    f"class={self.gd.class_name(self.class_type)!r} lvl={self.level} "
                    f"fame57={self.fame} fame39={self.account_fame} pos=({self.pos[0]:.1f},{self.pos[1]:.1f}) "
                    f"enemies={len(self.enemies)} quest={self.quest_oid} boss={self._current_boss()}")
