# RotMG Discord Rich Presence

Shows your Realm of the Mad God session on your Discord profile:

- character class and fame
- current area (Nexus, Vault, Realm, dungeons) with its portal icon
- the boss you're fighting, with its sprite and HP %

It reads a copy of the game's network traffic passively (via Npcap); nothing is
injected into the game client.

## Requirements

- Windows, Python 3.10+
- [Npcap](https://npcap.com) (installed with default options)
- Discord desktop app running

## Run

Double-click `run.bat` (installs `scapy` on first run), or:

```
pip install -r requirements.txt
python main.py            # add --debug to print the decoded game state
```

The presence syncs on the next map change after the tool starts.

## Configuration

Optional `config.json` next to `main.py` (any key overrides the default):

```json
{
  "client_id": "1557903491265986562",
  "icons_base_url": "https://raw.githubusercontent.com/LeoMeloL/rotmg-discord-rpc/main/icons/",
  "show_boss_hp": true,
  "boss_min_hp": 5000,
  "boss_radius": 15
}
```

## After a game update

- `python main.py --build-gamedata <objects.xml>` refreshes class/enemy data.
- `python extract_icons.py` re-extracts icons from the local game install
  (needs `pip install UnityPy Pillow`); commit the updated `icons/` folder.
- If packets stop decoding, packet ids live in `protocol.py` (`PACKET_IDS`,
  `STRING_STATS`).
