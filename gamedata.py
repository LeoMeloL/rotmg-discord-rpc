"""Compact lookup tables built from the game's objects.xml.

objects.xml is ~30 MB; the presence only needs class names and enemy info, so
`build()` distils it into gamedata.json (a few hundred KB) shipped with the tool.
Re-run `python main.py --build-gamedata <objects.xml>` after big game updates.
"""
from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "gamedata.json")

# Places where a full objects.xml usually exists on a machine that plays RotMG.
OBJECTS_XML_CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Realm Engine\resources\data\objects.xml"),
    os.path.join(HERE, "..", "client", "data", "objects.xml"),
    os.path.expandvars(r"%USERPROFILE%\Documents\Realmengine\data\objects.xml"),
]

# Fallback when gamedata.json is missing: class objectType -> name.
DEFAULT_CLASSES = {
    768: "Rogue", 775: "Archer", 782: "Wizard", 784: "Priest", 785: "Samurai",
    796: "Bard", 797: "Warrior", 798: "Knight", 799: "Paladin", 800: "Assassin",
    801: "Necromancer", 802: "Huntress", 803: "Mystic", 804: "Trickster",
    805: "Sorcerer", 806: "Ninja", 817: "Summoner", 818: "Kensei",
}

_TOKEN_RE = re.compile(r"^\{s\.([^}]+)\}$")


def prettify_name(raw: str) -> str:
    """'{s.some_token}' -> 'Some Token'; plain names pass through."""
    raw = (raw or "").strip()
    m = _TOKEN_RE.match(raw)
    if not m:
        return raw
    leaf = m.group(1).split(".")[-1]
    words = re.sub(r"[_-]+", " ", leaf).split()
    return " ".join(w[:1].upper() + w[1:] for w in words)


def _int(text: str | None, default: int = 0) -> int:
    if not text:
        return default
    try:
        return int(text.strip(), 0)
    except ValueError:
        return default


def build(xml_path: str, out_path: str = DEFAULT_PATH) -> dict:
    classes: dict[str, str] = {}
    enemies: dict[str, list] = {}
    for _, el in ET.iterparse(xml_path, events=("end",)):
        if el.tag != "Object":
            continue
        obj_type = _int(el.get("type"), -1)
        if obj_type < 0:
            el.clear()
            continue
        obj_id = el.get("id") or ""
        name = prettify_name(el.findtext("DisplayId") or "") or obj_id
        cls = (el.findtext("Class") or "").strip()
        if cls == "Player":
            classes[str(obj_type)] = name
        elif el.find("Enemy") is not None:
            flags = 0
            if el.find("Quest") is not None:
                flags |= 1
            if el.find("God") is not None:
                flags |= 2
            if el.find("Hero") is not None:
                flags |= 4
            enemies[str(obj_type)] = [name, _int(el.findtext("MaxHitPoints")), flags]
        el.clear()
    data = {"classes": classes, "enemies": enemies}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    return data


FLAG_QUEST = 1
FLAG_GOD = 2
FLAG_HERO = 4


class GameData:
    def __init__(self, path: str = DEFAULT_PATH):
        self.classes: dict[int, str] = dict(DEFAULT_CLASSES)
        self.enemies: dict[int, tuple[str, int, int]] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.classes.update({int(k): v for k, v in data.get("classes", {}).items()})
            self.enemies = {int(k): (v[0], v[1], v[2]) for k, v in data.get("enemies", {}).items()}

    def class_name(self, obj_type: int) -> str:
        return self.classes.get(obj_type, "")

    def enemy(self, obj_type: int) -> tuple[str, int, int] | None:
        return self.enemies.get(obj_type)


def find_objects_xml() -> str | None:
    for p in OBJECTS_XML_CANDIDATES:
        if os.path.isfile(p) and os.path.getsize(p) > 1_000_000:
            return os.path.abspath(p)
    return None
