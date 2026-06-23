#!/usr/bin/env python3
"""
Rocket League Transmogrifier — a friendly wrapper around RLUPKTools.

Drop this file next to rl_upk_editor.py, rl_asset_swapper.py, items.json,
and keys.txt. Run with:    python rl_modder.py
Or bundle with PyInstaller (see build_exe.bat).

This wrapper hides the technical details (donor/target/key dirs, header
offsets, AES, etc.) and exposes a simple "Replace [X] with [Y]" interface.
It auto-detects the user's Rocket League install (Steam + Epic) and tracks
applied swaps so the user can revert individual swaps without needing to
verify game files.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import sys
import threading
import traceback
import winreg
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ─── App constants ──────────────────────────────────────────────────────────

APP_NAME = "Rocket League Transmogrifier"
APP_VERSION = "1.2.70"

# Slots we expose in the main dropdown.
SUPPORTED_SLOTS = [
    "Boost",
    "Decal",
    "Wheels",
    "Trail",
]

EXPERIMENTAL_SLOTS = [
    "Antenna",
    "Avatar Border",
    "Engine Audio",
    "Goal Explosion",
    "Player Anthem",
    "Player Banner",
    "Topper",
]

# items.json uses "Rocket Boost"; map our display names to the data names where they differ.
SLOT_ALIASES: Dict[str, str] = {
    "Boost": "Rocket Boost",
}

# Slots that show a crash-risk warning.
# Swaps known to crash the game regardless of other settings.
# Each entry: (target_product | None, donor_product | None, message).
# None means "any" — so (None, "sparkles", ...) blocks Sparkles as donor against any target.
# Matched case-insensitively. Add more as they're confirmed.
KNOWN_BAD_SWAPS: List[Tuple[Optional[str], Optional[str], str]] = [
    (
        None,
        "sparkles",
        "Sparkles cannot be used as a replacement — this crashes the game on load.\n\n"
        "You can replace Sparkles with something else instead.",
    ),
]

class AutocompleteCombobox(ttk.Entry):
    def __init__(self, master, on_complete=None, **kwargs):
        # We inherit from Entry instead of Combobox to bypass the focus bug!
        super().__init__(master, **kwargs)
        self._completion_list = []
        self._listbox_win = None
        self._listbox = None
        self._root_click_id = None
        self._on_complete = on_complete  # called after a selection is confirmed

        self.bind('<KeyRelease>', self._on_keyrelease)
        self.bind('<ButtonRelease-1>', self._on_entry_click)
        self.bind('<Down>', self._on_arrow)
        self.bind('<Up>', self._on_arrow)
        self.bind('<Return>', self._on_return)

    def set_completion_list(self, completion_list):
        self._completion_list = completion_list

    def set(self, text):
        self.delete(0, tk.END)
        self.insert(0, text)

    def _on_entry_click(self, event):
        if not self._listbox_win:
            self._filter_and_show()

    def _on_keyrelease(self, event):
        if event.keysym == 'Escape':
            self._close_list()
            return
        if event.keysym in ('Up', 'Down', 'Return', 'Tab'):
            return
        self._filter_and_show()

    def _on_arrow(self, event):
        if not self._listbox_win:
            self._filter_and_show()
        if not self._listbox:
            return "break"
        size = self._listbox.size()
        if size == 0:
            return "break"
        cur = self._listbox.curselection()
        if event.keysym == 'Down':
            idx = (cur[0] + 1) if cur else 0
            idx = min(idx, size - 1)
        else:
            idx = (cur[0] - 1) if cur else 0
            idx = max(idx, 0)
        self._listbox.selection_clear(0, tk.END)
        self._listbox.selection_set(idx)
        self._listbox.see(idx)
        return "break"  # prevent cursor moving in the entry

    def _on_return(self, event):
        if self._listbox:
            sel = self._listbox.curselection()
            if sel:
                item = self._listbox.get(sel[0])
                self.delete(0, tk.END)
                self.insert(0, item)
                self._close_list()
                if self._on_complete:
                    self._on_complete()
                return "break"
        # No listbox open or nothing selected — treat as confirmation of typed text
        if self._on_complete:
            self._on_complete()
        return "break"

    def _filter_and_show(self):
        text = self.get()
        hits = self._completion_list if text == '' else [
            item for item in self._completion_list if text.lower() in item.lower()
        ]
        if hits:
            self._show_list(hits)
        else:
            self._close_list()

    def _show_list(self, hits):
        if not self._listbox_win:
            self._listbox_win = tk.Toplevel(self)
            self._listbox_win.wm_overrideredirect(True)
            self._listbox_win.attributes('-topmost', True)

            x = self.winfo_rootx()
            y = self.winfo_rooty() + self.winfo_height()
            w = self.winfo_width()
            self._listbox_win.geometry(f"{w}x150+{x}+{y}")

            self._listbox = tk.Listbox(
                self._listbox_win, bg="#1e1e1e", fg="#ffffff",
                selectbackground="#005fb8", relief="flat", highlightthickness=1,
                highlightbackground="#333333", activestyle='none', font=("Segoe UI", 9)
            )
            self._listbox.pack(fill=tk.BOTH, expand=True)
            self._listbox.bind('<ButtonRelease-1>', self._on_list_click)

            # Close when anything outside this widget or its listbox is clicked
            root = self.winfo_toplevel()
            self._root_click_id = root.bind('<Button-1>', self._on_root_click, '+')
        else:
            self._listbox.delete(0, tk.END)

        for item in hits:
            self._listbox.insert(tk.END, item)

    def _close_list(self, event=None):
        if self._root_click_id is not None:
            try:
                self.winfo_toplevel().unbind('<Button-1>', self._root_click_id)
            except Exception:
                pass
            self._root_click_id = None
        if self._listbox_win:
            self._listbox_win.destroy()
            self._listbox_win = None
            self._listbox = None

    def _on_root_click(self, event):
        # Close if the click landed outside this entry and its dropdown listbox
        if event.widget not in (self, self._listbox):
            self._close_list()

    def _on_list_click(self, event):
        if self._listbox:
            sel = self._listbox.curselection()
            if sel:
                item = self._listbox.get(sel[0])
                self.delete(0, tk.END)
                self.insert(0, item)
                self._close_list()
                if self._on_complete:
                    self._on_complete()


# ─── Path detection — Steam & Epic ──────────────────────────────────────────

def _read_reg(hive, path: str, name: str) -> Optional[str]:
    """Read a single registry value, returning None if anything fails."""
    try:
        with winreg.OpenKey(hive, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value) if value else None
    except OSError:
        return None


def detect_steam_installs() -> List[Path]:
    """Find Rocket League's CookedPCConsole via Steam registry + library folders."""
    found: List[Path] = []

    # 1. Direct uninstall key for Rocket League (App ID 252950).
    for hive_path in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 252950"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 252950"),
    ):
        loc = _read_reg(hive_path[0], hive_path[1], "InstallLocation")
        if loc:
            cooked = Path(loc) / "TAGame" / "CookedPCConsole"
            if cooked.exists():
                found.append(cooked)

    # 2. Steam's main install path → walk libraryfolders.vdf for additional drives.
    steam_path_str = _read_reg(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam", "SteamPath")
    if steam_path_str:
        steam_path = Path(steam_path_str.replace("/", "\\"))

        # Default library
        rl = steam_path / "steamapps" / "common" / "rocketleague" / "TAGame" / "CookedPCConsole"
        if rl.exists():
            found.append(rl)

        # Additional libraries from libraryfolders.vdf
        vdf = steam_path / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            try:
                text = vdf.read_text(encoding="utf-8", errors="ignore")
                # Crude but effective: pull every "path" line. Avoids needing a vdf parser.
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith('"path"'):
                        # Format: "path"  "C:\\SteamLibrary"
                        parts = line.split('"')
                        if len(parts) >= 4:
                            lib_path = Path(parts[3].replace("\\\\", "\\"))
                            rl = lib_path / "steamapps" / "common" / "rocketleague" / "TAGame" / "CookedPCConsole"
                            if rl.exists():
                                found.append(rl)
            except Exception:
                pass

    return found


def detect_epic_installs() -> List[Path]:
    """Find Rocket League via Epic Games Launcher manifests."""
    found: List[Path] = []
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    manifests_dir = Path(program_data) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"

    if not manifests_dir.exists():
        return found

    for manifest in manifests_dir.glob("*.item"):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            # Rocket League's Epic AppName is "Sugar". DisplayName check is a backup.
            app_name = data.get("AppName", "")
            display = data.get("DisplayName", "")
            if app_name == "Sugar" or "rocket league" in display.lower():
                install_loc = data.get("InstallLocation", "")
                if install_loc:
                    cooked = Path(install_loc) / "TAGame" / "CookedPCConsole"
                    if cooked.exists():
                        found.append(cooked)
        except Exception:
            continue

    return found


def detect_fallback_paths() -> List[Path]:
    """Common default install locations as a last resort."""
    candidates = [
        r"C:\Program Files (x86)\Steam\steamapps\common\rocketleague\TAGame\CookedPCConsole",
        r"C:\Program Files\Epic Games\rocketleague\TAGame\CookedPCConsole",
    ]
    return [Path(p) for p in candidates if Path(p).exists()]


def detect_all_installs() -> List[Path]:
    """Combined detection: Steam → Epic → defaults. De-duplicated, preserves order."""
    seen = set()
    results: List[Path] = []
    for path in (*detect_steam_installs(), *detect_epic_installs(), *detect_fallback_paths()):
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            results.append(path)
    return results


# ─── Module loading — find the bundled or sibling RLUPKTools scripts ────────

def app_dir() -> Path:
    """Return the directory we should look in for bundled assets."""
    if getattr(sys, "frozen", False):
        # PyInstaller: assets are extracted to sys._MEIPASS, but the .exe also
        # lives in a dir we can write to. Use _MEIPASS for read-only assets.
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def writable_dir() -> Path:
    """Where we store config/state. Survives between launches."""
    if getattr(sys, "frozen", False):
        # Use %APPDATA%\RLTM
        base = Path(os.environ.get("APPDATA", Path.home())) / "RLTM"
    else:
        base = Path(__file__).resolve().parent / ".modder_state"
    base.mkdir(parents=True, exist_ok=True)
    return base


def load_module_from(path: Path, module_name: str):
    """Dynamically load a .py file as a module."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_rlupk_modules():
    """Load rl_upk_editor and rl_asset_swapper from app_dir."""
    here = app_dir()
    editor_path = here / "rl_upk_editor.py"
    swapper_path = here / "rl_asset_swapper.py"

    if not editor_path.exists() or not swapper_path.exists():
        raise FileNotFoundError(
            f"Missing required scripts.\n\n"
            f"Looking in: {here}\n"
            f"Need: rl_upk_editor.py and rl_asset_swapper.py"
        )

    editor = load_module_from(editor_path, "rl_upk_editor")
    swapper = load_module_from(swapper_path, "rl_asset_swapper")
    return editor, swapper


# ─── State tracking — what's currently swapped ──────────────────────────────

@dataclass
class ActiveSwap:
    target_id: int
    donor_id: int
    target_product: str
    donor_product: str
    target_package: str
    slot: str

    @property
    def label(self) -> str:
        return f"{self.target_product}  →  {self.donor_product}  ({self.slot})"


def state_file() -> Path:
    return writable_dir() / "active_swaps.json"


def load_active_swaps() -> List[ActiveSwap]:
    p = state_file()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [ActiveSwap(**row) for row in data]
    except Exception:
        return []


def save_active_swaps(swaps: List[ActiveSwap]) -> None:
    state_file().write_text(
        json.dumps([asdict(s) for s in swaps], indent=2),
        encoding="utf-8",
    )


def config_file() -> Path:
    return writable_dir() / "config.json"


def load_config() -> Dict:
    p = config_file()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: Dict) -> None:
    config_file().write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def presets_file() -> Path:
    return writable_dir() / "presets.json"


def load_presets() -> Dict[str, List]:
    p = presets_file()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_presets(presets: Dict[str, List]) -> None:
    presets_file().write_text(json.dumps(presets, indent=2), encoding="utf-8")


# ─── Main GUI ───────────────────────────────────────────────────────────────

class ModderApp:
    def __init__(self, root: tk.Tk, editor_module, swapper_module):
        self.root = root
        self.editor = editor_module
        self.swapper = swapper_module

        self.items = []
        self.game_paths: List[Path] = []
        self.selected_game_path: Optional[Path] = None
        self.config = load_config()
        self.active_swaps: List[ActiveSwap] = load_active_swaps()
        self.presets: Dict[str, List] = load_presets()
        if "Default" not in self.presets:
            self.presets["Default"] = []
            save_presets(self.presets)

        # Slot -> [items in that slot]
        self.slot_items: Dict[str, List] = {}

        self._build_ui()
        self._load_items()
        self._detect_game_path()
        self._refresh_active_list()
        self._refresh_preset_combo()

    # ── UI construction ──

    def _build_ui(self) -> None:
        self.root.title(APP_NAME)
        self.root.geometry("720x820")
        self.root.minsize(640, 900)

        # Modern-ish ttk theme
        style = ttk.Style()
        
        # Using 'clam' first actually gives you more control over a "flat" modern look
        # than the older Windows native themes, but we'll keep the fallbacks.
        for theme in ("clam", "vista", "winnative"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except tk.TclError:
                    continue

        # Define a sleek, low-contrast color palette (Modern Light Mode)
        bg_color = "#f3f4f6"        # Soft off-white background
        text_primary = "#111827"    # Near-black for readability
        text_secondary = "#6b7280"  # Soft gray for subtext
        accent_color = "#2563eb"    # Modern vibrant blue for active states

        # Style the main background so it isn't the harsh Windows default gray
        style.configure("TFrame", background=bg_color)

        # Typography & Labels: Softer backgrounds, better hierarchy
        style.configure("Header.TLabel", font=("Segoe UI", 16, "bold"), foreground=text_primary, background=bg_color)
        style.configure("Sub.TLabel", font=("Segoe UI", 10), foreground=text_secondary, background=bg_color)
        style.configure("Status.TLabel", font=("Segoe UI", 9), foreground=text_primary, background=bg_color)
        # Buttons: Increased padding for a "chunkier", more clickable modern feel
        style.configure("Apply.TButton", font=("Segoe UI", 10, "bold"), padding=(24, 12))

        # Interactive states: This adds a visual change when the user hovers over buttons
        style.map("Apply.TButton",
            foreground=[('pressed', text_primary), ('active', accent_color)]
        )

        # Increased outer padding for more "breathing room" (a staple of modern UI)
        outer = ttk.Frame(self.root, padding=24, style="TFrame")
        outer.pack(fill="both", expand=True)

        # Header
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(header, text=APP_NAME, style="Header.TLabel").pack(side="left")
        ttk.Label(header, text=f"v{APP_VERSION}", style="Sub.TLabel").pack(side="left", padx=(8, 0), pady=(6, 0))

        # Game path status (auto-detected, with a 'change' button)
        path_row = ttk.Frame(outer)
        path_row.pack(fill="x", pady=(0, 12))
        self.path_status = ttk.Label(path_row, text="Detecting Rocket League…", style="Status.TLabel")
        self.path_status.pack(side="left")
        ttk.Button(path_row, text="Change", command=self._pick_path_manually, width=10).pack(side="right")

        # Custom swap section
        ttk.Label(outer, text="Custom Swap", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Pick a slot, then choose an item to replace and what to replace it with.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(0, 6))

        # Slot picker
        slot_row = ttk.Frame(outer)
        slot_row.pack(fill="x", pady=(0, 4))
        ttk.Label(slot_row, text="Slot:", width=8).pack(side="left")
        self.slot_var = tk.StringVar(value=SUPPORTED_SLOTS[0])
        self.slot_combo = ttk.Combobox(
            slot_row,
            values=SUPPORTED_SLOTS + EXPERIMENTAL_SLOTS,
            textvariable=self.slot_var,
            state="readonly",
            width=24,
        )
        self.slot_combo.pack(side="left")
        self.slot_combo.bind("<<ComboboxSelected>>", self._on_slot_change)

        # Target & donor dropdowns
        picker = ttk.Frame(outer)
        picker.pack(fill="x", pady=(8, 8))

        ttk.Label(picker, text="Replace:", width=10).grid(row=0, column=0, sticky="w", pady=4)
        self.target_var = tk.StringVar()
        self.target_combo = AutocompleteCombobox(
            picker, textvariable=self.target_var,
            on_complete=lambda: self._advance_to_donor(),
        )
        self.target_combo.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(picker, text="With:", width=10).grid(row=1, column=0, sticky="w", pady=4)
        self.donor_var = tk.StringVar()
        self.donor_combo = AutocompleteCombobox(
            picker, textvariable=self.donor_var,
            on_complete=lambda: self.donor_combo._close_list(),
        )
        self.donor_combo.grid(row=1, column=1, sticky="ew", pady=4)

        picker.columnconfigure(1, weight=1)

        # Preserve header offsets toggle
        self.preserve_var = tk.BooleanVar(value=False)
        preserve_row = ttk.Frame(outer)
        preserve_row.pack(fill="x", pady=(4, 2))
        ttk.Checkbutton(
            preserve_row,
            text="Preserve header offsets",
            variable=self.preserve_var,
        ).pack(side="left")
        ttk.Label(
            preserve_row,
            text="Toggle this if a swap crashes your game.",
            style="Sub.TLabel",
        ).pack(side="left", padx=(8, 0))

        # Apply button
        apply_row = ttk.Frame(outer)
        apply_row.pack(fill="x", pady=(8, 14))
        self.apply_btn = ttk.Button(
            apply_row,
            text="Apply Swap",
            style="Apply.TButton",
            command=self._apply_custom_swap,
        )
        self.apply_btn.pack(side="left")

        self.busy_label = ttk.Label(apply_row, text="", style="Sub.TLabel")
        self.busy_label.pack(side="left", padx=(12, 0))

        # Active swaps list
        ttk.Separator(outer, orient="horizontal").pack(fill="x", pady=(4, 10))
        ttk.Label(outer, text="Active Swaps", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Swaps you've applied. Click Revert to undo a single swap, or use Steam/Epic 'verify game files' to reset everything.",
            style="Sub.TLabel",
            wraplength=680,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))

        list_frame = ttk.Frame(outer)
        list_frame.pack(fill="both", expand=True)
        self.active_listbox = tk.Listbox(list_frame, activestyle="dotbox", height=8)
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.active_listbox.yview)
        self.active_listbox.configure(yscrollcommand=sb.set)
        self.active_listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        action_row = ttk.Frame(outer)
        action_row.pack(fill="x", pady=(8, 0))
        ttk.Button(action_row, text="Clear Selected", command=self._revert_selected).pack(side="left")
        ttk.Button(action_row, text="Clear All", command=self._clear_active_list).pack(side="left", padx=(8, 0))

        # ── Preset save/load ──
        ttk.Separator(outer, orient="horizontal").pack(fill="x", pady=(10, 8))
        ttk.Label(outer, text="Presets", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="Select a preset to apply it. Active swaps auto-save to the selected preset.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(0, 6))

        preset_ctrl_row = ttk.Frame(outer)
        preset_ctrl_row.pack(fill="x")

        self.preset_var = tk.StringVar()
        self.preset_combo = ttk.Combobox(
            preset_ctrl_row,
            textvariable=self.preset_var,
            state="readonly",
            width=28,
        )
        self.preset_combo.pack(side="left")
        self.preset_combo.bind("<<ComboboxSelected>>", lambda _: self._load_preset())

        ttk.Button(preset_ctrl_row, text="Save As", command=self._save_preset).pack(side="left", padx=(6, 0))
        ttk.Button(preset_ctrl_row, text="Rename", command=self._rename_preset).pack(side="left", padx=(4, 0))
        ttk.Button(preset_ctrl_row, text="Delete", command=self._delete_preset).pack(side="left", padx=(4, 0))

        self.preset_busy_label = ttk.Label(outer, text="", style="Sub.TLabel")
        self.preset_busy_label.pack(anchor="w", pady=(4, 0))

    # ── Items / slots ──

    def _load_items(self) -> None:
        items_path = app_dir() / "items.json"
        if not items_path.exists():
            messagebox.showerror(APP_NAME, f"items.json not found.\n\nExpected at: {items_path}")
            self.root.destroy()
            return
        try:
            self.items = self.swapper.load_items(items_path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not load items.json:\n\n{exc}")
            self.root.destroy()
            return

        # Bucket by slot (display name → items). SLOT_ALIASES maps display→data names.
        for slot in SUPPORTED_SLOTS + EXPERIMENTAL_SLOTS:
            data_slot = SLOT_ALIASES.get(slot, slot)
            self.slot_items[slot] = sorted(
                [it for it in self.items if it.slot == data_slot],
                key=lambda x: x.product.lower(),
            )

        self._on_slot_change()

    def _advance_to_donor(self) -> None:
        """Called when Enter is pressed in the Replace box — move focus to With."""
        self.donor_combo.focus_set()
        self.donor_combo._filter_and_show()

    def _on_slot_change(self, *_) -> None:
        slot = self.slot_var.get()
        items = self.slot_items.get(slot, [])
        labels = [self._item_label(it) for it in items]
        self.target_combo.set_completion_list(labels)
        self.donor_combo.set_completion_list(labels)
        self.target_var.set("")
        self.donor_var.set("")

    @staticmethod
    def _item_label(item) -> str:
        # Friendly format: "Octane (Common)" — hides ID and filename from user
        if item.quality:
            return f"{item.product}  ·  {item.quality}"
        return item.product

    def _resolve_label_to_item(self, slot: str, label: str):
        for it in self.slot_items.get(slot, []):
            if self._item_label(it) == label:
                return it
        return None

    def _resolve_id_to_item(self, item_id: int):
        for it in self.items:
            if it.id == item_id:
                return it
        return None

    # ── Path detection ──

    def _detect_game_path(self) -> None:
        # If user previously picked one, prefer that
        saved = self.config.get("game_path")
        if saved and Path(saved).exists():
            self.game_paths = [Path(saved)]
            self.selected_game_path = Path(saved)
            self._update_path_status()
            return

        self.game_paths = detect_all_installs()
        if not self.game_paths:
            self.path_status.config(
                text="✗ Rocket League not found automatically — click Change to pick the folder.",
                foreground="#b00020",
            )
        elif len(self.game_paths) == 1:
            self.selected_game_path = self.game_paths[0]
            self.config["game_path"] = str(self.selected_game_path)
            save_config(self.config)
            self._update_path_status()
        else:
            self._prompt_pick_game_path()

    def _prompt_pick_game_path(self) -> None:
        win = tk.Toplevel(self.root)
        win.title(f"{APP_NAME} — Multiple installations detected")
        win.geometry("640x300")
        win.transient(self.root)
        win.grab_set()

        ttk.Label(
            win,
            text="Multiple Rocket League installations found.\nSelect which one to use:",
        ).pack(pady=(14, 8))

        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        lb = tk.Listbox(frame, activestyle="dotbox", exportselection=False)
        lb.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=lb.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        lb.configure(yscrollcommand=scroll.set)

        for gp in self.game_paths:
            root_str = str(gp.parents[1]) if len(gp.parents) >= 2 else str(gp)
            lb.insert(tk.END, root_str)

        def on_select() -> None:
            sel = lb.curselection()
            if not sel:
                return
            self.selected_game_path = self.game_paths[sel[0]]
            self.config["game_path"] = str(self.selected_game_path)
            save_config(self.config)
            self._update_path_status()
            win.destroy()

        ttk.Button(win, text="Select", command=on_select).pack(pady=(0, 12))
        lb.bind("<Double-Button-1>", lambda _: on_select())

        self.root.wait_window(win)

    def _update_path_status(self) -> None:
        if not self.selected_game_path:
            return
        # Try to show the install root, not the deep CookedPCConsole path
        root_str = str(self.selected_game_path.parents[1]) if len(self.selected_game_path.parents) >= 2 else str(self.selected_game_path)
        self.path_status.config(
            text=f"✓ Rocket League found: {root_str}",
            foreground="#2d7a2d",
        )

    def _pick_path_manually(self) -> None:
        chosen = filedialog.askdirectory(
            title="Select your Rocket League CookedPCConsole folder",
            mustexist=True,
        )
        if not chosen:
            return
        path = Path(chosen)
        if path.name != "CookedPCConsole":
            # Try to be helpful — maybe they picked the install root
            candidate = path / "TAGame" / "CookedPCConsole"
            if candidate.exists():
                path = candidate
            else:
                if not messagebox.askyesno(
                    APP_NAME,
                    "That folder isn't named 'CookedPCConsole'. Use it anyway?",
                ):
                    return
        self.selected_game_path = path
        self.game_paths = [path]
        self.config["game_path"] = str(path)
        save_config(self.config)
        self._update_path_status()

    # ── Active list ──

    def _refresh_active_list(self) -> None:
        self.active_listbox.delete(0, tk.END)
        for swap in self.active_swaps:
            self.active_listbox.insert(tk.END, swap.label)

    def _clear_active_list(self) -> None:
        if not self.active_swaps:
            return

        if not self.selected_game_path or not self.selected_game_path.exists():
            messagebox.showerror(APP_NAME, "Game folder not set — cannot revert files.")
            return

        errors: List[str] = []
        no_backup: List[str] = []

        for swap in self.active_swaps:
            original = self.selected_game_path / swap.target_package
            backup = original.with_suffix(original.suffix + ".bak")
            if backup.exists():
                try:
                    if original.exists():
                        original.unlink()
                    backup.rename(original)
                except Exception as exc:
                    errors.append(f"{swap.target_product}: {exc}")
            else:
                no_backup.append(swap.target_product)

        self.active_swaps = []
        save_active_swaps(self.active_swaps)
        self._refresh_active_list()
        self._auto_save_to_preset()

        if errors or no_backup:
            parts = []
            if no_backup:
                parts.append("No backup found (use 'Verify game files' to restore):\n" + "\n".join(f"  • {n}" for n in no_backup))
            if errors:
                parts.append("Errors during revert:\n" + "\n".join(f"  • {e}" for e in errors))
            messagebox.showwarning(APP_NAME, "List cleared, but some files could not be reverted:\n\n" + "\n\n".join(parts))

    def _revert_selected(self) -> None:
        sel = self.active_listbox.curselection()
        if not sel:
            return
        swap = self.active_swaps[sel[0]]

        if not self.selected_game_path:
            messagebox.showerror(APP_NAME, "Game folder not set.")
            return

        # Check for .bak file in the game folder
        original = self.selected_game_path / swap.target_package
        backup = original.with_suffix(original.suffix + ".bak")

        if backup.exists():
            try:
                # Replace the modded file with the backup
                if original.exists():
                    original.unlink()
                backup.rename(original)

                self.active_swaps.pop(sel[0])
                save_active_swaps(self.active_swaps)
                self._refresh_active_list()
                self._auto_save_to_preset()
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Revert failed:\n\n{exc}")
        else:
            msg = (
                f"No backup file found for {swap.target_package}.\n\n"
                f"To restore the original, right-click Rocket League in Steam/Epic "
                f"and choose 'Verify integrity of game files'.\n\n"
                f"Remove this swap from the list?"
            )
            if messagebox.askyesno(APP_NAME, msg):
                self.active_swaps.pop(sel[0])
                save_active_swaps(self.active_swaps)
                self._refresh_active_list()
                self._auto_save_to_preset()

    # ── User presets ──

    def _refresh_preset_combo(self) -> None:
        names = sorted(self.presets.keys())
        self.preset_combo["values"] = names
        current = self.preset_var.get()
        if current not in names:
            self.preset_var.set("Default" if "Default" in names else (names[0] if names else ""))

    def _auto_save_to_preset(self) -> None:
        """If a preset is currently selected, silently update it with the current active swaps."""
        name = self.preset_var.get()
        if name and name in self.presets:
            self.presets[name] = [asdict(s) for s in self.active_swaps]
            save_presets(self.presets)

    def _save_preset(self) -> None:
        """Save As — prompts for a new name and saves current active swaps under it."""
        self._prompt_name_and_save(initial=self.preset_var.get())

    def _rename_preset(self) -> None:
        """Rename the currently selected preset."""
        old_name = self.preset_var.get()
        if not old_name or old_name not in self.presets:
            return
        self._prompt_name_and_save(initial=old_name, delete_old=old_name)

    def _prompt_name_and_save(self, initial: str = "", delete_old: Optional[str] = None) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Save As" if not delete_old else "Rename preset")
        dlg.resizable(False, False)
        dlg.grab_set()

        ttk.Label(dlg, text="Preset name:", padding=(12, 12, 12, 4)).pack(anchor="w")
        name_var = tk.StringVar(value=initial)
        entry = ttk.Entry(dlg, textvariable=name_var, width=32)
        entry.pack(padx=12, pady=(0, 8))
        entry.selection_range(0, tk.END)
        entry.focus_set()

        def _do_save():
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning(APP_NAME, "Enter a name.", parent=dlg)
                return
            if name != delete_old and name in self.presets:
                if not messagebox.askyesno(APP_NAME, f"Overwrite existing preset '{name}'?", parent=dlg):
                    return
            if delete_old and delete_old != name:
                self.presets.pop(delete_old, None)
            self.presets[name] = [asdict(s) for s in self.active_swaps]
            save_presets(self.presets)
            self._refresh_preset_combo()
            self.preset_var.set(name)
            dlg.destroy()

        btn_row = ttk.Frame(dlg)
        btn_row.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(btn_row, text="Save", command=_do_save).pack(side="left")
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side="left", padx=(6, 0))
        entry.bind("<Return>", lambda _: _do_save())

    def _delete_preset(self) -> None:
        name = self.preset_var.get()
        if not name:
            return
        if not messagebox.askyesno(APP_NAME, f"Delete preset '{name}'?"):
            return
        self.presets.pop(name, None)
        save_presets(self.presets)
        self._refresh_preset_combo()

    def _load_preset(self) -> None:
        name = self.preset_var.get()
        if not name or name not in self.presets:
            return
        if not self.selected_game_path or not self.selected_game_path.exists():
            messagebox.showerror(APP_NAME, "Game folder not set. Click 'Change' to set it first.")
            return

        preset_swaps = self.presets[name]
        if not preset_swaps:
            return

        self._set_preset_busy(True, f"Loading preset '{name}'…")
        threading.Thread(
            target=self._load_preset_worker,
            args=(name, preset_swaps),
            daemon=True,
        ).start()

    def _load_preset_worker(self, name: str, preset_swaps: List[dict]) -> None:
        errors: List[str] = []

        # Step 1 — revert all current active swaps
        for swap in list(self.active_swaps):
            original = self.selected_game_path / swap.target_package
            backup = original.with_suffix(original.suffix + ".bak")
            if backup.exists():
                try:
                    if original.exists():
                        original.unlink()
                    backup.rename(original)
                except Exception as exc:
                    errors.append(f"Revert {swap.target_product}: {exc}")
        self.active_swaps = []

        # Step 2 — apply each swap in the preset
        keys_path = app_dir() / "keys.txt"
        for swap_dict in preset_swaps:
            target = self._resolve_id_to_item(swap_dict["target_id"])
            donor = self._resolve_id_to_item(swap_dict["donor_id"])
            if not target or not donor:
                errors.append(f"Item IDs {swap_dict.get('target_id')} / {swap_dict.get('donor_id')} not found in items.json")
                continue

            target_file = self.selected_game_path / target.asset_package
            if not target_file.exists():
                errors.append(f"{target.asset_package} not found in game folder — skipped")
                continue

            try:
                options = self.swapper.SwapOptions(
                    items_path=app_dir() / "items.json",
                    keys_path=keys_path,
                    donor_dir=self.selected_game_path,
                    output_dir=self.selected_game_path,
                    key_source_dir=self.selected_game_path,
                    include_thumbnails=False,
                    preserve_header_offsets=self.preserve_var.get(),
                    overwrite=True,
                )
                self.swapper.swap_asset(
                    self.swapper.import_rl_upk_editor() if hasattr(self.swapper, "import_rl_upk_editor") else self.editor,
                    target, donor, options,
                )
                self.active_swaps.append(ActiveSwap(
                    target_id=target.id,
                    donor_id=donor.id,
                    target_product=target.product,
                    donor_product=donor.product,
                    target_package=target.asset_package,
                    slot=target.slot,
                ))
            except Exception as exc:
                errors.append(f"{target.product} → {donor.product}: {exc}")

        save_active_swaps(self.active_swaps)
        self.root.after(0, self._on_preset_load_done, name, errors)

    def _on_preset_load_done(self, name: str, errors: List[str]) -> None:
        self._set_preset_busy(False)
        self._refresh_active_list()
        if errors:
            detail = "\n".join(f"• {e}" for e in errors)
            messagebox.showwarning(
                APP_NAME,
                f"Preset '{name}' loaded with {len(self.active_swaps)} swap(s), but {len(errors)} error(s) occurred:\n\n{detail}",
            )
        pass

    def _set_preset_busy(self, busy: bool, message: str = "") -> None:
        if busy:
            self.preset_combo.state(["disabled"])
            self.preset_busy_label.config(text=message)
        else:
            self.preset_combo.state(["!disabled"])
            self.preset_busy_label.config(text="")

    # ── The actual swap operation ──

    def _apply_custom_swap(self) -> None:
        slot = self.slot_var.get()
        target = self._resolve_label_to_item(slot, self.target_var.get())
        donor = self._resolve_label_to_item(slot, self.donor_var.get())
        if not target or not donor:
            return
        if target.id == donor.id:
            return

        # Check against known-bad swap combinations
        ta = target.product.lower()
        da = donor.product.lower()
        for bad_target, bad_donor, msg in KNOWN_BAD_SWAPS:
            target_match = bad_target is None or ta == bad_target
            donor_match = bad_donor is None or da == bad_donor
            if target_match and donor_match:
                messagebox.showerror(APP_NAME, f"Known incompatible swap\n\n{msg}")
                return

        self._run_swap(target, donor)

    def _run_swap(self, target, donor) -> None:
        if not self.selected_game_path or not self.selected_game_path.exists():
            messagebox.showerror(
                APP_NAME,
                "Rocket League folder not set.\n\nClick 'Change' to point to your CookedPCConsole folder.",
            )
            return

        # Check the target file actually exists in the game folder
        target_file = self.selected_game_path / target.asset_package
        if not target_file.exists():
            messagebox.showerror(
                APP_NAME,
                f"Couldn't find {target.asset_package} in your game folder.\n\n"
                f"Either the folder is wrong, or this item isn't in your install.",
            )
            return

        # Run the swap on a worker thread so the UI doesn't freeze
        self._set_busy(True, f"Swapping {target.product} → {donor.product}…")
        threading.Thread(
            target=self._swap_worker,
            args=(target, donor),
            daemon=True,
        ).start()

    def _swap_worker(self, target, donor) -> None:
        try:
            keys_path = app_dir() / "keys.txt"
            if not keys_path.exists():
                raise FileNotFoundError(f"keys.txt not found at {keys_path}")

            options = self.swapper.SwapOptions(
                items_path=app_dir() / "items.json",
                keys_path=keys_path,
                donor_dir=self.selected_game_path,    # Read donor from game folder
                output_dir=self.selected_game_path,   # Write output to same folder
                key_source_dir=self.selected_game_path,  # Use existing target's key
                include_thumbnails=False,
                preserve_header_offsets=self.preserve_var.get(),
                overwrite=True,  # We don't keep .bak in this UI to save disk
            )

            _, log = self.swapper.swap_asset(
                self.swapper.import_rl_upk_editor() if hasattr(self.swapper, "import_rl_upk_editor") else self.editor,
                target,
                donor,
                options,
            )

            # Track the swap
            new_swap = ActiveSwap(
                target_id=target.id,
                donor_id=donor.id,
                target_product=target.product,
                donor_product=donor.product,
                target_package=target.asset_package,
                slot=target.slot,
            )
            # Replace any existing swap on the same target
            self.active_swaps = [s for s in self.active_swaps if s.target_id != target.id]
            self.active_swaps.append(new_swap)
            save_active_swaps(self.active_swaps)

            self.root.after(0, self._on_swap_success, target, donor)
        except Exception as exc:
            tb = traceback.format_exc()
            self.root.after(0, self._on_swap_error, str(exc), tb)

    def _on_swap_success(self, target, donor) -> None:
        self._set_busy(False)
        self._refresh_active_list()
        self._auto_save_to_preset()

    def _on_swap_error(self, msg: str, traceback_text: str) -> None:
        self._set_busy(False)
        # Keep the user-facing message short; full traceback only for technical users
        short = msg.splitlines()[0] if msg else "Unknown error"
        if messagebox.askyesno(
            APP_NAME,
            f"Swap failed:\n\n{short}\n\nShow technical details?",
        ):
            self._show_traceback_dialog(traceback_text)

    def _show_traceback_dialog(self, text: str) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Error details")
        dlg.geometry("700x420")
        txt = tk.Text(dlg, wrap="word", bg="#1a1a1a", fg="#dddddd")
        sb = ttk.Scrollbar(dlg, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        txt.insert("1.0", text)
        txt.configure(state="disabled")

    def _set_busy(self, busy: bool, message: str = "") -> None:
        if busy:
            self.apply_btn.state(["disabled"])
            self.busy_label.config(text=message)
        else:
            self.apply_btn.state(["!disabled"])
            self.busy_label.config(text="")


# ─── Entry point ────────────────────────────────────────────────────────────

def _apply_icon(root: tk.Tk, ico: Path) -> None:
    """Apply ico to the title bar and taskbar. Deferred so the HWND is stable."""
    def _set():
        # iconbitmap works when the file exists (dev mode or after bundling with datas)
        if ico.exists():
            try:
                root.iconbitmap(str(ico))
            except Exception:
                pass

        try:
            hwnd = int(root.wm_frame(), 16)
            # Prefer loading from file; fall back to extracting from the exe itself
            if ico.exists():
                hicon = ctypes.windll.user32.LoadImageW(
                    None, str(ico), 1, 0, 0, 0x10 | 0x40
                )
            else:
                # Frozen exe: icon is embedded in the binary, extract it directly
                hicon = ctypes.windll.shell32.ExtractIconW(0, sys.executable, 0)
            if hicon:
                ctypes.windll.user32.SendMessageW(hwnd, 0x80, 0, hicon)  # ICON_SMALL
                ctypes.windll.user32.SendMessageW(hwnd, 0x80, 1, hicon)  # ICON_BIG
        except Exception:
            pass
    root.after(0, _set)


def main() -> int:
    # Tell Windows to stop auto-scaling the app
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    # Set AppUserModelID so Windows uses our icon in the taskbar instead of Python's
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RLTM.RocketLeagueTransmogrifier.1")
    except Exception:
        pass

    try:
        editor, swapper = load_rlupk_modules()
    except Exception as exc:
        # We need a Tk root to show a messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, f"Failed to start:\n\n{exc}")
        return 1

    root = tk.Tk()

    ico = app_dir() / "default.ico"
    _apply_icon(root, ico)

    ModderApp(root, editor, swapper)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())