#!/usr/bin/env python3
"""
RLTM Item Adder — scan CookedPCConsole for new items and add them to items.json.
Run: python item_adder.py
"""

import json
import tkinter as tk
import urllib.request
import urllib.error
from pathlib import Path
from tkinter import filedialog, ttk

VELOCITY_ITEMS_URL = "https://api.velocityrl.tech/items.json"

QUALITIES = ["Uncommon", "Common", "Rare", "Very Rare", "Import", "Exotic", "Black Market", "Limited", "Premium"]

SLOT_HINTS = [
    ("Explosion_", "Goal Explosion"), ("explosion_", "Goal Explosion"),
    ("Boost_",     "Rocket Boost"),   ("boost_",     "Rocket Boost"),
    ("WHEEL_",     "Wheels"),         ("Wheel_",     "Wheels"),
    ("SS_",        "Trail"),          ("Trail_",     "Trail"),
    ("Hat_",       "Topper"),         ("Topper_",    "Topper"),
    ("Antenna_",   "Antenna"),
    ("Skin_",      "Decal"),          ("Decal_",     "Decal"),
    ("PlayerBanner_",  "Player Banner"),
    ("avatarborder_",  "Avatar Border"), ("AvatarBorder_", "Avatar Border"),
    ("Body_",      "Body"),
    ("EngineAudio_",   "Engine Audio"),
    ("PaintFinish_",   "Paint Finish"),
    ("title_",     "Player Title"),
    ("MX_",        "Player Anthem"),
]

def guess_slot(name):
    for prefix, slot in SLOT_HINTS:
        if name.startswith(prefix):
            return slot
    return ""

def stem(name):
    s = Path(name).stem
    return s[:-3] if s.upper().endswith("_SF") else s

def infer_path(name):
    s = stem(name)
    return f"{s}.{s}"

def load_items(path):
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    rows = raw.get("Items", raw if isinstance(raw, list) else [])
    known = {str(r.get("AssetPackage") or "").lower() for r in rows}
    return raw, rows, known

def save_items(path, raw, rows):
    if isinstance(raw, list):
        path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    else:
        raw["Items"] = rows
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

def fetch_velocity_catalog():
    """Download VelocityRL's items.json and return a dict keyed by AssetPackage (lowercase)."""
    try:
        print(f"[api] Fetching {VELOCITY_ITEMS_URL} ...")
        req = urllib.request.Request(VELOCITY_ITEMS_URL, headers={"User-Agent": "RLTM-ItemAdder/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        rows = raw.get("Items") or raw.get("items") or (raw if isinstance(raw, list) else [])
        catalog = {
            str(r.get("AssetPackage") or "").lower(): r
            for r in rows if r.get("AssetPackage")
        }
        print(f"[api] Got {len(catalog)} items from VelocityRL catalog.")
        return catalog
    except urllib.error.URLError as e:
        print(f"[api] Could not reach VelocityRL API: {e}")
        return {}
    except Exception as e:
        print(f"[api] Unexpected error fetching catalog: {e}")
        return {}


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("RLTM Item Adder")
        self.root.geometry("500x380")
        self.root.resizable(False, False)

        self.items_path = None
        self.raw = None
        self.rows = []
        self.unknown = []
        self.idx = 0
        self.catalog = {}  # VelocityRL catalog keyed by AssetPackage.lower()

        # Paths row
        pf = ttk.Frame(root, padding=10)
        pf.pack(fill="x")
        self.items_var = tk.StringVar()
        self.game_var = tk.StringVar()

        ttk.Label(pf, text="items.json:").grid(row=0, column=0, sticky="w")
        ttk.Entry(pf, textvariable=self.items_var, width=38).grid(row=0, column=1, padx=4)
        ttk.Button(pf, text="…", width=2, command=self._pick_items).grid(row=0, column=2)

        ttk.Label(pf, text="CookedPCConsole:").grid(row=1, column=0, sticky="w", pady=(4,0))
        ttk.Entry(pf, textvariable=self.game_var, width=38).grid(row=1, column=1, padx=4, pady=(4,0))
        ttk.Button(pf, text="…", width=2, command=self._pick_game).grid(row=1, column=2, pady=(4,0))

        ttk.Button(pf, text="Scan for new items", command=self._scan).grid(row=2, column=0, columnspan=3, pady=(8,0))

        ttk.Separator(root, orient="horizontal").pack(fill="x")

        # Item form
        ff = ttk.Frame(root, padding=10)
        ff.pack(fill="both", expand=True)
        ff.columnconfigure(1, weight=1)

        self.progress_var = tk.StringVar(value="Scan to begin.")
        ttk.Label(ff, textvariable=self.progress_var, foreground="#555").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0,8))

        labels = ["AssetPackage", "AssetPath", "Slot", "Product name", "ID", "Quality"]
        self.vars = {k: tk.StringVar() for k in labels}

        for i, key in enumerate(["AssetPackage", "AssetPath", "Slot"]):
            ttk.Label(ff, text=f"{key}:").grid(row=i+1, column=0, sticky="w", pady=3)
            ttk.Entry(ff, textvariable=self.vars[key], state="readonly").grid(row=i+1, column=1, sticky="ew", pady=3)

        ttk.Label(ff, text="Product name:").grid(row=4, column=0, sticky="w", pady=3)
        self.product_entry = ttk.Entry(ff, textvariable=self.vars["Product name"])
        self.product_entry.grid(row=4, column=1, sticky="ew", pady=3)

        ttk.Label(ff, text="ID:").grid(row=5, column=0, sticky="w", pady=3)
        ttk.Entry(ff, textvariable=self.vars["ID"]).grid(row=5, column=1, sticky="ew", pady=3)

        ttk.Label(ff, text="Quality:").grid(row=6, column=0, sticky="w", pady=3)
        ttk.Combobox(ff, textvariable=self.vars["Quality"], values=QUALITIES, state="readonly", width=20).grid(row=6, column=1, sticky="w", pady=3)
        self.vars["Quality"].set("Uncommon")

        # Buttons
        bf = ttk.Frame(root, padding=(10, 0, 10, 10))
        bf.pack(fill="x")
        ttk.Button(bf, text="Skip", command=self._skip).pack(side="left")
        ttk.Button(bf, text="Add →", command=self._add).pack(side="right")

        self._auto_detect()

    def _auto_detect(self):
        here = Path(__file__).resolve().parent
        for p in (here / "items.json", here.parent / "items.json"):
            if p.exists():
                self.items_var.set(str(p))
                break

    def _pick_items(self):
        p = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if p:
            self.items_var.set(p)

    def _pick_game(self):
        p = filedialog.askdirectory(title="Select CookedPCConsole")
        if p:
            self.game_var.set(p)

    def _scan(self):
        ip = self.items_var.get().strip()
        gp = self.game_var.get().strip()
        if not ip or not gp:
            self.progress_var.set("Set both paths first.")
            return

        self.items_path = Path(ip)
        self.raw, self.rows, known = load_items(self.items_path)
        game_path = Path(gp)

        self.unknown = [
            f.name for f in sorted(game_path.glob("*_SF.upk"))
            if not f.name.lower().endswith("_t_sf.upk")
            and f.name.lower() not in known
            and guess_slot(f.name) != ""  # skip files that don't match any cosmetic prefix
        ]

        print(f"[scan] {len(self.unknown)} new file(s) found")
        for n in self.unknown:
            print(f"  {n}")

        self.catalog = fetch_velocity_catalog()

        self.idx = 0
        self._show_current()

    def _show_current(self):
        if self.idx >= len(self.unknown):
            self.progress_var.set("✓ All done.")
            for k in ["AssetPackage", "AssetPath", "Slot", "Product name", "ID"]:
                self.vars[k].set("")
            self.vars["Quality"].set("Uncommon")
            print("[done] No more new items.")
            return

        name = self.unknown[self.idx]
        self.vars["AssetPackage"].set(name)
        self.vars["AssetPath"].set(infer_path(name))
        self.vars["Slot"].set(guess_slot(name))
        self.vars["Product name"].set("")
        self.vars["ID"].set("")
        self.vars["Quality"].set("Uncommon")

        # Try to fill from VelocityRL catalog
        match = self.catalog.get(name.lower())
        if match:
            self.vars["Product name"].set(str(match.get("Product") or ""))
            self.vars["ID"].set(str(match.get("ID") or ""))
            self.vars["Quality"].set(str(match.get("Quality") or "Uncommon"))
            slot = str(match.get("Slot") or "")
            if slot:
                self.vars["Slot"].set(slot)
            asset_path = str(match.get("AssetPath") or "")
            if asset_path:
                self.vars["AssetPath"].set(asset_path)
            self.progress_var.set(f"Item {self.idx + 1} of {len(self.unknown)}: {name}  ✓ auto-filled from VelocityRL")
            print(f"[api] Auto-filled: {match.get('Product')} | {match.get('Slot')} | {match.get('Quality')} | ID={match.get('ID')}")
        else:
            self.progress_var.set(f"Item {self.idx + 1} of {len(self.unknown)}: {name}  (not in VelocityRL catalog — fill manually)")
            print(f"[api] No catalog match for {name} — manual entry needed")

        self.product_entry.focus_set()

    def _add(self):
        product = self.vars["Product name"].get().strip()
        item_id  = self.vars["ID"].get().strip()
        quality  = self.vars["Quality"].get().strip()

        if not product:
            self.progress_var.set("Enter a product name.")
            return
        if not item_id.isdigit():
            self.progress_var.set("Enter a numeric ID.")
            return

        entry = {
            "AssetPackage":  self.vars["AssetPackage"].get(),
            "AssetPath":     self.vars["AssetPath"].get(),
            "ID":            int(item_id),
            "Product":       product,
            "Quality":       quality,
            "Slot":          self.vars["Slot"].get(),
            "UnlockMethod":  "UnlockMethod_Online",
        }

        self.rows.append(entry)
        save_items(self.items_path, self.raw, self.rows)

        print(f"[added] {entry['Slot']} | {entry['Product']} | {entry['Quality']} | ID={entry['ID']} | {entry['AssetPackage']}")

        self.idx += 1
        self._show_current()

    def _skip(self):
        if self.idx < len(self.unknown):
            print(f"[skip]  {self.unknown[self.idx]}")
            self.idx += 1
            self._show_current()


def _apply_icon(root, ico):
    try:
        root.iconbitmap(str(ico))
    except Exception:
        pass
    try:
        import ctypes
        hwnd = int(root.wm_frame(), 16)
        hicon = ctypes.windll.user32.LoadImageW(
            None, str(ico), 1, 0, 0, 0x10 | 0x40
        )
        if hicon:
            ctypes.windll.user32.SendMessageW(hwnd, 0x80, 0, hicon)
            ctypes.windll.user32.SendMessageW(hwnd, 0x80, 1, hicon)
    except Exception:
        pass


def main():
    print("RLTM Item Adder starting...")
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RLTM.ItemAdder.1")
    except Exception:
        pass
    root = tk.Tk()
    ico = Path(__file__).resolve().parent / "default.ico"
    if ico.exists():
        _apply_icon(root, ico)
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
