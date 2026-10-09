"""RotMG Discord Rich Presence.

Usage:
  python main.py                       run the presence
  python main.py --debug               also print the decoded game state every 2s
  python main.py --build-gamedata [objects.xml]   rebuild gamedata.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time

import gamedata
from discord_ipc import DiscordIPC
from state import GameState, Snapshot

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
ICONS_MANIFEST = os.path.join(HERE, "icons", "manifest.json")
GAME_EXE = "RotMG Exalt.exe"

DEFAULT_CONFIG = {
    "client_id": "1557903491265986562",
    # Public folder holding icons/ (see extract_icons.py). Empty = no icons.
    "icons_base_url": "https://raw.githubusercontent.com/LeoMeloL/rotmg-discord-rpc/main/icons/",
    # Big image when no area icon applies (menus); a Developer Portal asset key or URL.
    "large_image": "",
    "use_class_images": True,
    "show_boss_hp": True,
    # Discord appends "(N of MAX)" to the area line.
    "show_player_count": True,
    "boss_min_hp": 5000,
    "boss_radius": 15,
    "interfaces": [],
}

log = logging.getLogger("rpc")


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def game_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
    except OSError:
        return True
    return GAME_EXE.lower() in out.lower()


def _clip(text: str) -> str:
    """Discord requires 2..128 chars for text fields."""
    text = text.strip()
    if len(text) > 128:
        text = text[:127] + "…"
    return text if len(text) >= 2 else text + "  "


class Icons:
    """Maps classes/bosses/areas to public icon URLs via icons/manifest.json."""

    def __init__(self, base_url: str, manifest_path: str = ICONS_MANIFEST):
        self.base = base_url.rstrip("/") + "/" if base_url else ""
        self.manifest: dict = {}
        if self.base and os.path.exists(manifest_path):
            with open(manifest_path, encoding="utf-8") as f:
                self.manifest = json.load(f)

    def url(self, section: str, *keys) -> str:
        table = self.manifest.get(section, {})
        for key in keys:
            rel = table.get(str(key).lower())
            if rel:
                return self.base + rel
        return ""


def build_activity(s: Snapshot, cfg: dict, traffic_seen: bool, icons: Icons) -> dict:
    default_image = cfg["large_image"] or icons.url("areas", "realm")
    assets = {"large_text": "Realm of the Mad God"}
    if default_image:
        assets["large_image"] = default_image
    if not s.in_game:
        # Traffic on port 2050 we could not sync to (tool started mid-session).
        return {"details": "In game" if traffic_seen else "In the menus", "assets": assets}

    area = s.area
    area_image = icons.url("areas", area, s.map_name) or default_image
    if area_image:
        assets["large_image"] = area_image
    assets["large_text"] = _clip(area)
    if not s.class_name:  # map just loaded, character not decoded yet
        return {"details": _clip(area), "assets": assets}
    cls = s.class_name

    details = f"{cls} • {s.fame:,} Fame"
    if s.boss:
        # 0% while alive = invulnerable/scripted phase without an HP bar: hide it.
        hp = f" ({s.boss_hp_pct}%)" if cfg["show_boss_hp"] and s.boss_hp_pct > 0 else ""
        # During a scripted encounter the phase name replaces the area (still in the image tooltip).
        state = f"⚔ {s.boss}{hp} • {s.phase or area}"
        boss_image = icons.url("bosses", s.boss_type)
        if boss_image:
            assets["large_image"] = boss_image
            assets["large_text"] = _clip(f"{s.boss} • {area}")
    elif s.phase:
        state = f"⚔ {s.phase} • {area}"
    else:
        state = area

    class_image = icons.url("classes", s.class_type)
    if cfg["use_class_images"] and class_image:
        assets["small_image"] = class_image
        assets["small_text"] = _clip(f"{cls} • Lv {s.level}" if s.level else cls)

    activity = {"details": _clip(details), "state": _clip(state), "assets": assets}
    if cfg["show_player_count"] and s.max_players > 0 and s.players > 0:
        activity["party"] = {"id": f"rotmg-{s.map_name}", "size": [min(s.players, s.max_players), s.max_players]}
    if s.area_since:
        activity["timestamps"] = {"start": int(s.area_since)}
    return activity


def _without_party(activity: dict | None) -> dict | None:
    if activity is None:
        return None
    return {k: v for k, v in activity.items() if k != "party"}


def run(cfg: dict, debug: bool):
    from sniffer import Sniffer  # imported late: scapy start-up is slow

    gd_path = gamedata.DEFAULT_PATH
    if not os.path.exists(gd_path):
        xml = gamedata.find_objects_xml()
        if xml:
            log.info("Building gamedata.json from %s", xml)
            gamedata.build(xml, gd_path)
        else:
            log.warning("gamedata.json not found: boss detection disabled, class names use fallback")
    gd = gamedata.GameData(gd_path)
    log.info("Game data: %d classes, %d enemies", len(gd.classes), len(gd.enemies))

    icons = Icons(cfg["icons_base_url"])
    log.info("Icons: %s", ", ".join(f"{len(v)} {k}" for k, v in icons.manifest.items()) or "disabled")

    state = GameState(gd, boss_min_hp=int(cfg["boss_min_hp"]), boss_radius=float(cfg["boss_radius"]))
    sniffer = Sniffer(state.on_packet, state.on_flow_closed, cfg["interfaces"] or None)
    sniffer.start()
    log.info("Waiting for a map change to sync (enter the Nexus, Vault, a realm or a dungeon)")

    ipc = DiscordIPC(str(cfg["client_id"]))
    last_activity: dict | None = None
    last_push = 0.0
    last_connect_try = 0.0
    running, last_running_check = False, 0.0
    last_debug = 0.0

    while True:
        now = time.time()
        if now - last_running_check > 10:
            running, last_running_check = game_running(), now

        traffic_seen = now - sniffer.last_traffic < 10
        activity = build_activity(state.snapshot(), cfg, traffic_seen, icons) if running else None

        if not ipc.connected and now - last_connect_try > 15:
            last_connect_try = now
            if ipc.connect():
                last_activity, last_push = None, 0.0
                if activity is None:
                    ipc.set_activity(None)

        # Discord allows ~5 updates / 20 s; push only on change, at most every 5 s.
        # Player counts churn constantly (Nexus), so a count-only change waits 15 s.
        min_gap = 15 if _without_party(activity) == _without_party(last_activity) else 5
        if ipc.connected and activity != last_activity and now - last_push >= min_gap:
            if ipc.set_activity(activity):
                last_activity, last_push = activity, now
                log.info("Presence: %s", "cleared" if activity is None else
                         f"{activity.get('details')} | {activity.get('state', '')}")

        if debug and now - last_debug >= 2:
            last_debug = now
            print("[debug]", state.debug_line(), flush=True)
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser(description="RotMG Discord Rich Presence")
    ap.add_argument("--debug", action="store_true", help="print decoded game state every 2 s")
    ap.add_argument("--build-gamedata", nargs="?", const="", metavar="OBJECTS_XML",
                    help="rebuild gamedata.json from objects.xml and exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(name)-8s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("scapy").setLevel(logging.ERROR)

    if args.build_gamedata is not None:
        xml = args.build_gamedata or gamedata.find_objects_xml()
        if not xml:
            sys.exit("objects.xml not found; pass its path: --build-gamedata C:\\path\\objects.xml")
        data = gamedata.build(xml)
        print(f"gamedata.json: {len(data['classes'])} classes, {len(data['enemies'])} enemies")
        return

    try:
        run(load_config(), args.debug)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
