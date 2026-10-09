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


def _join_names(names: list[str]) -> str:
    """["A"] -> "A"; ["A", "B", "C"] -> "A, B & C"."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " & " + names[-1]


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
    phase: str = ""     # encounter phase banner, e.g. "Fireworks Display (Climax)"
    class_type: int = 0
    players: int = 0
    max_players: int = 0
    difficulty: float = -1.0
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
        # The server sends every player in the map (not just nearby ones).
        self.players: set[int] = set()
        self.max_players = 0
        self.difficulty = -1.0
        self.closed_at = 0.0
        self.last_boss = ("", -1, 0)
        self.last_boss_seen = 0.0
        self.packets_seen = 0
        # Scripted encounters (e.g. Moonlight Village): bosses announce themselves
        # in chat, then the server shows phase banners. Timed with the server clock.
        self.server_ms = 0
        self.speakers: list[tuple[int, int]] = []  # (server_ms, oid) of big enemies that spoke
        self.encounter_oids: list[int] = []
        self.encounter_names: list[str] = []
        self.encounter_types: list[int] = []
        self.phase = ""
        self.intensity = ""
        self.solo_bosses: list[tuple[str, int]] = []  # (name, type) in the order they were fought

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
                self.server_ms, statuses = P.parse_newtick(body)
                self._on_newtick(statuses)
            elif name == "QUESTOBJECTID":
                self.quest_oid = P.parse_int32(body)
            elif name == "TEXT" and self.map_name in self.ENCOUNTER_MAPS:
                self._on_text(*P.parse_text(body))
            elif name == "NOTIFICATION" and self.map_name in self.ENCOUNTER_MAPS:
                self._on_notification(*P.parse_notification(body))

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
        self.players.clear()
        self.max_players = m["max_players"]
        self.difficulty = m["difficulty"]
        self.speakers.clear()
        self.solo_bosses.clear()
        self._end_encounter()
        self.last_boss = ("", -1, 0)  # don't carry the previous map's boss over
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
            if obj_type in self.gd.classes:
                self.players.add(oid)
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
            self.players.discard(oid)

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

    # Moonlight Village is the only dungeon whose bosses work as a scripted show:
    # no HP bars, no quest target; each boss speaks, then phase banners follow.
    # Every other dungeon uses the regular quest-boss detection.
    ENCOUNTER_MAPS = {"Moonlight Village"}
    # Finales get a short label: the 3-boss dance is named after the last solo
    # boss ("Miko Finale"), Umi's last phase is always this banner ("Umi Finale").
    UMI_FINALE_PHASE = "Fireworks on a Starless Night"
    SPEECH_BURST_MS = 1500   # bosses that speak together (same line) share the encounter
    SPEECH_MAX_AGE_MS = 120_000

    def _big_enemy(self, oid: int) -> tuple[str, int] | None:
        """(name, objectType) if oid is a tracked enemy with boss-level HP."""
        e = self.enemies.get(oid)
        if e is None:
            return None
        name, xml_hp, _ = self.gd.enemy(e[0])
        if max(xml_hp, e[4]) < self.boss_min_hp:
            return None
        return name, e[0]

    def _on_text(self, sender: str, oid: int, text: str):
        if sender.startswith("#") and self._big_enemy(oid):
            self.speakers.append((self.server_ms, oid))
            del self.speakers[:-20]

    def _on_notification(self, kind: int, text: str):
        if kind == P.NOTIF_PHASE and text:
            self._start_phase(text)
        elif kind == P.NOTIF_INTENSITY and text and self.phase:
            self.intensity = text
            log.info("Phase intensity: %s", text)
        elif kind == P.NOTIF_ENCOUNTER_END and self.phase:
            log.info("Encounter finished")
            self._end_encounter()

    def _start_phase(self, phase: str):
        recent = [(t, oid) for t, oid in self.speakers
                  if self.server_ms - t <= self.SPEECH_MAX_AGE_MS and self._big_enemy(oid)]
        if recent:
            last = max(t for t, _ in recent)
            oids: list[int] = []
            for t, oid in recent:
                if last - t <= self.SPEECH_BURST_MS and oid not in oids:
                    oids.append(oid)
            self.encounter_oids = oids
            infos = [self._big_enemy(o) for o in oids]
            self.encounter_names = [i[0] for i in infos]
            self.encounter_types = [i[1] for i in infos]
            if len(infos) == 1 and (not self.solo_bosses or self.solo_bosses[-1] != infos[0]):
                self.solo_bosses.append(infos[0])
        self.phase = phase
        self.intensity = ""
        log.info("Phase: %s (%s)", phase, ", ".join(self.encounter_names) or "boss unknown")

    def _end_encounter(self):
        """Clears the active encounter (solo_bosses is kept: the finale needs it)."""
        self.encounter_oids, self.encounter_names, self.encounter_types = [], [], []
        self.phase = ""
        self.intensity = ""
        self.last_boss = ("", -1, 0)

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
            phase = ""
            if self.phase:  # scripted encounter takes precedence over proximity
                phase = f"{self.phase} ({self.intensity})" if self.intensity else self.phase
                if self.encounter_names:
                    # These bosses have no HP bar, so no HP % either.
                    boss, boss_type, pct = _join_names(self.encounter_names), self.encounter_types[0], -1
                    finale_of = None
                    if len(self.encounter_names) > 1 and self.solo_bosses:
                        finale_of = self.solo_bosses[-1]
                    elif self.phase == self.UMI_FINALE_PHASE:
                        finale_of = (self.encounter_names[0], self.encounter_types[0])
                    if finale_of:
                        # "Dancer Miko" -> "Miko Finale"; the intensity replaces the phase name
                        boss, boss_type = f"{finale_of[0].split()[-1]} Finale", finale_of[1]
                        phase = self.intensity
            elif boss:
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
                phase=phase,
                class_type=self.class_type,
                players=len(self.players),
                max_players=self.max_players,
                difficulty=self.difficulty,
                area_since=self.area_since,
            )

    def debug_line(self) -> str:
        with self.lock:
            return (f"pkts={self.packets_seen} area={self.area!r} realm={self.realm!r} oid={self.my_oid} "
                    f"class={self.gd.class_name(self.class_type)!r} lvl={self.level} "
                    f"fame57={self.fame} fame39={self.account_fame} pos=({self.pos[0]:.1f},{self.pos[1]:.1f}) "
                    f"players={len(self.players)}/{self.max_players} enemies={len(self.enemies)} quest={self.quest_oid} boss={self._current_boss()}")
