# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**Rocket League Transmogrifier (RLTM)** — a Windows GUI tool that lets players swap cosmetic assets (boosts, wheels, decals, etc.) in Rocket League without touching raw game files manually. It wraps `rl_upk_editor.py` and `rl_asset_swapper.py` (the RLUPKTools engine) with a friendly tkinter interface.

## Running the app

```
python RLTM.py
```

Requires all four sibling files to be present: `rl_upk_editor.py`, `rl_asset_swapper.py`, `items.json`, `keys.txt`.

## Building the distributable EXE

```
pyinstaller RLTM.spec
```

Output lands at `dist/RLTM.exe`. The spec bundles the four dependency files inside the exe (extracted to `sys._MEIPASS` at runtime). UPX is disabled intentionally to avoid AV false positives.

## Architecture

Three-layer design:

| Layer | File(s) | Role |
|---|---|---|
| GUI wrapper | `RLTM.py` | All user-facing logic; no UPK knowledge |
| Swap engine | `rl_asset_swapper.py` | Orchestrates asset swaps; defines `Item`, `SwapOptions`, `swap_asset()`, `load_items()` |
| UPK parser | `rl_upk_editor.py` | Low-level AES-encrypted `.upk` binary read/write |

`RLTM.py` dynamically loads the other two via `importlib` (`load_rlupk_modules()`) rather than static imports. This is intentional — it lets the same modules work as standalone CLI tools and as bundled assets inside the PyInstaller exe.

### Key runtime paths

- **`app_dir()`** — read-only assets (`items.json`, `keys.txt`, the two `.py` modules). Points to `sys._MEIPASS` when frozen, `__file__`'s directory otherwise.
- **`writable_dir()`** — mutable state. Points to `%APPDATA%\RLTM` when frozen, `.modder_state/` beside the script otherwise.
- State is stored as JSON: `active_swaps.json` (swap history) and `config.json` (last game path).

### Swap flow

1. `ModderApp._run_swap()` validates the game path and target file exist, then spawns a daemon thread.
2. `_swap_worker()` calls `swapper.swap_asset(editor, target, donor, options)` with `donor_dir = output_dir = game_path` (in-place modification).
3. On success the swap is appended to `active_swaps` and persisted. Revert works by restoring a `.bak` file the engine writes alongside the modified `.upk`.

### Game path detection order

Steam registry (uninstall key → libraryfolders.vdf) → Epic manifest files (`%ProgramData%\Epic\...\Manifests\*.item`, AppName `"Sugar"`) → hardcoded fallback paths. User override is saved to `config.json` and takes priority on next launch.

### `AutocompleteCombobox`

Custom `ttk.Entry` subclass (not `ttk.Combobox`) that spawns a floating `tk.Toplevel` listbox for item search. The Entry base was chosen specifically to work around a focus bug in `ttk.Combobox` on Windows.

## Data files

- **`items.json`** — catalogue of every swappable item: `id`, `product`, `quality`, `slot`, `asset_package`, `asset_path`. Parsed by `rl_asset_swapper.load_items()` into `Item` dataclasses.
- **`keys.txt`** — AES decryption keys for `.upk` files, consumed by `rl_upk_editor`.

## Slots and presets

`SUPPORTED_SLOTS` in `RLTM.py` is the authoritative list of slots shown in the UI. `RISKY_SLOTS` triggers a warning label (currently only `"Goal Explosion"`). Hard-coded presets live in the `PRESETS` list and reference items by their `items.json` integer ID.
