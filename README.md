# Rocket League Transmogrifier (RLTM)

A Windows tool for swapping cosmetic assets in Rocket League — change how your boosts, decals, wheels, and more look in-game without touching raw files manually.

## Download

Grab the latest `RLTM.exe` from the [Releases](../../releases) page. No install needed — just run it.

## How it works

RLTM wraps the [RLUPKTools](https://github.com/CrunchyRL/RLUPKTools) engine, which modifies the encrypted `.upk` game files that Rocket League loads for each cosmetic item. It auto-detects your Rocket League install (Steam and Epic), tracks every swap you've applied, and lets you revert individual swaps from within the app.

A `.bak` backup is written next to each modified file before it's changed. If something goes wrong, you can revert from RLTM or use Steam/Epic's **Verify game files** to fully reset.

## Usage

1. Launch `RLTM.exe`
2. Confirm your Rocket League install was detected (or click **Change** to set it manually)
3. Pick a slot (Boost, Decal, Wheels, Trail)
4. Search and select what you want to **replace**, then what to replace it **with**
5. Click **Apply Swap**, then restart Rocket League

**Enable other swaps** exposes less-tested slots (Goal Explosion, Topper, etc.) — these have a higher chance of crashing on load. Use **Revert** if that happens.

## Building from source

Requires Python 3.11+ and PyInstaller.

```
pip install pyinstaller cryptography
pyinstaller RLTM.spec
```

Output: `dist/RLTM.exe`

The spec bundles `rl_upk_editor.py`, `rl_asset_swapper.py`, `items.json`, `keys.txt`, and `default.ico` inside the exe. UPX is intentionally disabled to avoid antivirus false positives.

## Files

| File | Purpose |
|---|---|
| `RLTM.py` | GUI wrapper — all user-facing logic |
| `rl_asset_swapper.py` | Swap engine — orchestrates UPK modifications |
| `rl_upk_editor.py` | Low-level AES-encrypted `.upk` binary read/write |
| `items.json` | Catalogue of every swappable item |
| `keys.txt` | AES decryption keys for `.upk` files |
| `item_adder.py` | Maintenance tool for adding new items to `items.json` |

## Credits

Built on [RLUPKTools](https://github.com/CrunchyRL/RLUPKTools) by CrunchyRL.
Collision-handling fix contributed by the RLUPKTools community.
