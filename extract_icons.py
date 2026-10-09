"""Extracts presence icons (classes, bosses, dungeons) from the local game install.

Discord only shows images from public HTTPS URLs, so the output folder
(icons/) is meant to be committed to the project's public host; main.py then
references icons by URL using icons/manifest.json.

  python extract_icons.py [--game-dir "...\\RotMG Exalt_Data"] [--objects objects.xml]

Needs: pip install UnityPy Pillow
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import xml.etree.ElementTree as ET

import UnityPy
from PIL import Image

import gamedata

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "icons")
ICON_SIZE = 256

GAME_DATA_CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\RealmOfTheMadGod\Production\RotMG Exalt_Data"),
    r"C:\Program Files (x86)\Steam\steamapps\common\Realm of the Mad God\RotMG Exalt_Data",
]
# spritesheetf atlas ids -> Texture2D names in resources.assets
ATLAS_NAMES = {1: "groundTiles", 2: "characters", 3: "characters_masks", 4: "mapObjects"}
# Areas without a dungeon portal: area name (as shown by the presence) -> portal object id
FIXED_AREAS = {"Nexus": "Nexus Portal", "Vault": "Vault Portal", "Realm": "Realm Portal"}


class SpriteSheet:
    """Reader for the game's `spritesheetf` FlatBuffer.

    root: f0 = [SpriteGroup{f0 name, f1 atlasId, f2 [Sprite]}],
          f1 = [AnimatedSprite{f0 name, f1 index, f2 set, f3 direction, f4 action, f5 Sprite}]
    Sprite: f0 = struct {x, y, w, h: float}, f3 = index, f7 = atlasId
    """

    def __init__(self, data: bytes):
        self.b = data
        root = self._u32(0)
        self.static: dict[tuple[str, int], tuple] = {}
        for g in self._vec(root, 0):
            name = self._str(g, 0).lower()
            for s in self._vec(g, 2):
                self.static.setdefault((name, self._int(s, 3)), self._rect(s))
        # (name, index) -> best (score, rect); action 0 = standing, direction 3 = facing front
        self.animated: dict[tuple[str, int], tuple] = {}
        for a in self._vec(root, 1):
            key = (self._str(a, 0).lower(), self._int(a, 1))
            action, direction = self._int(a, 4), self._int(a, 3)
            score = (action != 0, direction not in (3, 0), direction != 3)
            best = self.animated.get(key)
            if best is None or score < best[0]:
                self.animated[key] = (score, self._rect(self._table(a, 5)))

    def _u32(self, o): return struct.unpack_from("<I", self.b, o)[0]
    def _i32(self, o): return struct.unpack_from("<i", self.b, o)[0]
    def _u16(self, o): return struct.unpack_from("<H", self.b, o)[0]

    def _field(self, t, k):
        vt = t - self._i32(t)
        if 4 + 2 * k >= self._u16(vt):
            return 0
        off = self._u16(vt + 4 + 2 * k)
        return t + off if off else 0

    def _int(self, t, k):
        a = self._field(t, k)
        return self._i32(a) if a else 0

    def _str(self, t, k):
        a = self._field(t, k)
        if not a:
            return ""
        s = a + self._u32(a)
        return self.b[s + 4:s + 4 + self._u32(s)].decode("utf-8", "replace")

    def _table(self, t, k):
        a = self._field(t, k)
        return a + self._u32(a) if a else 0

    def _vec(self, t, k):
        a = self._field(t, k)
        if not a:
            return []
        v = a + self._u32(a)
        return [v + 4 + 4 * i + self._u32(v + 4 + 4 * i) for i in range(self._u32(v))]

    def _rect(self, s):
        a = self._field(s, 0)
        x, y, w, h = struct.unpack_from("<4f", self.b, a) if a else (0, 0, 0, 0)
        return self._int(s, 7), int(x), int(y), int(w), int(h)

    def find(self, file: str, index: int, animated: bool):
        key = (file.lower(), index)
        if animated and key in self.animated:
            return self.animated[key][1]
        return self.static.get(key) or (self.animated.get(key) or (None, None))[1]


def load_game_assets(game_dir: str):
    env = UnityPy.load(os.path.join(game_dir, "resources.assets"))
    sheet, atlases = None, {}
    wanted = set(ATLAS_NAMES.values())
    for obj in env.objects:
        if obj.type.name == "TextAsset":
            d = obj.read()
            if d.m_Name == "spritesheetf":
                raw = d.m_Script
                sheet = raw.encode("utf-8", "surrogateescape") if isinstance(raw, str) else bytes(raw)
        elif obj.type.name == "Texture2D":
            d = obj.read()
            if d.m_Name in wanted and d.m_Name not in atlases:
                atlases[d.m_Name] = d.image.convert("RGBA")
    if sheet is None:
        raise SystemExit("spritesheetf not found in resources.assets")
    return SpriteSheet(sheet), atlases


def render_icon(atlas: Image.Image, rect) -> Image.Image | None:
    _, x, y, w, h = rect
    if w <= 0 or h <= 0:
        return None
    sprite = atlas.crop((x, y, x + w, y + h))
    bbox = sprite.getbbox()  # trim transparent padding
    if not bbox:
        return None
    sprite = sprite.crop(bbox)
    scale = max(1, (ICON_SIZE - 32) // max(sprite.size))  # integer scale keeps pixels crisp
    sprite = sprite.resize((sprite.width * scale, sprite.height * scale), Image.NEAREST)
    canvas = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    canvas.paste(sprite, ((ICON_SIZE - sprite.width) // 2, (ICON_SIZE - sprite.height) // 2))
    return canvas


def texture_of(el):
    for tag, animated in (("AnimatedTexture", True), ("Texture", False)):
        t = el.find(tag)
        if t is not None and t.findtext("File"):
            try:
                return t.findtext("File").strip(), int((t.findtext("Index") or "0").strip(), 0), animated
            except ValueError:
                return None
    return None


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-dir", help='folder containing resources.assets ("RotMG Exalt_Data")')
    ap.add_argument("--objects", help="objects.xml path")
    args = ap.parse_args()

    game_dir = args.game_dir or next((d for d in GAME_DATA_CANDIDATES
                                      if os.path.isfile(os.path.join(d, "resources.assets"))), None)
    objects_xml = args.objects or gamedata.find_objects_xml()
    if not game_dir or not objects_xml:
        raise SystemExit("game folder or objects.xml not found; pass --game-dir / --objects")

    print("Loading game assets from", game_dir)
    sheet, atlases = load_game_assets(game_dir)
    print(f"  {len(sheet.static)} static + {len(sheet.animated)} animated sprites, atlases: {sorted(atlases)}")

    manifest = {"classes": {}, "bosses": {}, "areas": {}}
    portals_by_id: dict[str, tuple] = {}
    jobs = []  # (manifest section, key, relative path, texture)
    for _, el in ET.iterparse(objects_xml, events=("end",)):
        if el.tag != "Object":
            continue
        obj_id = el.get("id") or ""
        cls = (el.findtext("Class") or "").strip()
        tex = texture_of(el)
        try:
            obj_type = int(el.get("type") or "", 0)
        except ValueError:
            obj_type = -1
        if tex and obj_type >= 0:
            if cls == "Player":
                jobs.append(("classes", str(obj_type), f"class/{obj_type}.png", tex))
            elif el.find("Enemy") is not None and el.find("Quest") is not None:
                jobs.append(("bosses", str(obj_type), f"boss/{obj_type}.png", tex))
            elif cls == "Portal":
                portals_by_id[obj_id] = tex
                dungeon = (el.findtext("DungeonName") or "").strip()
                if dungeon:
                    jobs.append(("areas", dungeon.lower(), f"area/{slug(dungeon)}.png", tex))
        el.clear()
    for area, portal_id in FIXED_AREAS.items():
        if portal_id in portals_by_id:
            jobs.append(("areas", area.lower(), f"area/{slug(area)}.png", portals_by_id[portal_id]))

    missing = 0
    for section, key, rel, (file, index, animated) in jobs:
        if key in manifest[section]:
            continue
        rect = sheet.find(file, index, animated)
        atlas = atlases.get(ATLAS_NAMES.get(rect[0], "")) if rect else None
        icon = render_icon(atlas, rect) if atlas is not None else None
        if icon is None:
            missing += 1
            continue
        path = os.path.join(OUT_DIR, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        icon.save(path, optimize=True)
        manifest[section][key] = rel

    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=0, sort_keys=True)
    print({k: len(v) for k, v in manifest.items()}, f"| {missing} without a sprite")


if __name__ == "__main__":
    main()
