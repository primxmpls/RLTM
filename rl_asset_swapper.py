#!/usr/bin/env python3
import argparse
import base64
import importlib
import importlib.util
import json
import queue
import shutil
import struct
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


@dataclass(frozen=True)
class Item:
    id: int
    product: str
    quality: str
    slot: str
    asset_package: str
    asset_path: str

    @property
    def package_stem(self) -> str:
        return Path(self.asset_package).stem

    @property
    def asset_parts(self) -> List[str]:
        return [p for p in self.asset_path.split(".") if p]

    @property
    def asset_base(self) -> str:
        parts = self.asset_parts
        return parts[0] if parts else self.package_stem.removesuffix("_SF")

    @property
    def thumbnail_package(self) -> str:
        return f"{self.asset_base}_T_SF.upk"

    @property
    def label(self) -> str:
        quality = f" / {self.quality}" if self.quality else ""
        slot = f" / {self.slot}" if self.slot else ""
        return f"[{self.id}] {self.product}{quality}{slot} ({self.asset_package})"


@dataclass
class SwapOptions:
    items_path: Path
    keys_path: Optional[Path]
    donor_dir: Path
    output_dir: Path
    key_source_dir: Optional[Path]
    include_thumbnails: bool
    preserve_header_offsets: bool
    overwrite: bool
    logger: Optional[Callable[[str], None]] = None


def script_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_path(names: Sequence[str]) -> Path:
    here = script_dir()
    for name in names:
        p = here / name
        if p.exists():
            return p
    return here / names[0]


def import_rl_upk_editor():
    try:
        return importlib.import_module("rl_upk_editor")
    except Exception:
        pass

    here = script_dir()
    candidates = [
        here / "rl_upk_editor.py",
        here / "rl_upk_editor(1).py",
        Path.cwd() / "rl_upk_editor.py",
        Path.cwd() / "rl_upk_editor(1).py",
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        spec = importlib.util.spec_from_file_location("rl_upk_editor", candidate)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules["rl_upk_editor"] = module
        spec.loader.exec_module(module)
        return module

    raise ImportError("Put this script next to rl_upk_editor.py or rl_upk_editor(1).py")


def load_items(path: Path) -> List[Item]:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    rows = raw.get("Items", raw if isinstance(raw, list) else [])
    out: List[Item] = []
    for row in rows:
        try:
            pkg = str(row.get("AssetPackage", "") or "")
            asset_path = str(row.get("AssetPath", "") or "")
            if not pkg or not asset_path:
                continue
            out.append(Item(
                id=int(row.get("ID", 0) or 0),
                product=str(row.get("Product", "") or ""),
                quality=str(row.get("Quality", "") or ""),
                slot=str(row.get("Slot", "") or ""),
                asset_package=pkg,
                asset_path=asset_path,
            ))
        except Exception:
            continue
    out.sort(key=lambda x: (x.slot.lower(), x.product.lower(), x.id))
    return out


def find_item(items: Sequence[Item], value: str, slot: str = "") -> Item:
    value = str(value).strip()
    rows = [x for x in items if not slot or x.slot.lower() == slot.lower()]
    if value.isdigit():
        wanted = int(value)
        matches = [x for x in rows if x.id == wanted]
    else:
        q = value.lower()
        matches = [x for x in rows if q in x.product.lower() or q in x.asset_package.lower() or q in x.asset_path.lower()]
    if not matches:
        raise ValueError(f"No item matched {value!r}" + (f" in slot {slot!r}" if slot else ""))
    if len(matches) > 1:
        exact = [x for x in matches if x.product.lower() == value.lower() or x.asset_package.lower() == value.lower()]
        if len(exact) == 1:
            return exact[0]
        raise ValueError("Ambiguous item match:\n" + "\n".join(x.label for x in matches[:20]))
    return matches[0]


def add_pair(pairs: List[Tuple[str, str]], old: str, new: str) -> None:
    old = (old or "").strip()
    new = (new or "").strip()
    if not old or not new or old == new:
        return
    if (old, new) not in pairs:
        pairs.append((old, new))


def infer_name_pairs(target: Item, donor: Item) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    donor_parts = donor.asset_parts
    target_parts = target.asset_parts
    if len(donor_parts) == len(target_parts):
        for old, new in zip(donor_parts, target_parts):
            add_pair(pairs, old, new)
    else:
        if donor_parts and target_parts:
            add_pair(pairs, donor_parts[0], target_parts[0])
            add_pair(pairs, donor_parts[-1], target_parts[-1])
        for old, new in zip(donor_parts, target_parts):
            add_pair(pairs, old, new)
    add_pair(pairs, donor.package_stem, target.package_stem)
    return pairs


def infer_thumbnail_pairs(target: Item, donor: Item) -> List[Tuple[str, str]]:
    return [
        (f"{donor.asset_base}_T", f"{target.asset_base}_T"),
        (f"{donor.asset_base}_T_SF", f"{target.asset_base}_T_SF"),
    ]


def clean_name(text: str) -> str:
    return str(text).split("\x00", 1)[0].strip()


def find_name_indices(package, name: str) -> Tuple[List[int], bool]:
    exact = [n.index for n in package.names if clean_name(n.name) == name]
    if exact:
        return exact, False
    q = name.lower()
    fuzzy = [n.index for n in package.names if clean_name(n.name).lower() == q]
    return fuzzy, bool(fuzzy)


def name_exists(package, name: str) -> bool:
    return bool(find_name_indices(package, name)[0])


def _walk_name_table(data: bytes, name_offset: int, name_count: int) -> List[Tuple[int, int, str]]:
    entries: List[Tuple[int, int, str]] = []
    pos = name_offset
    for _ in range(name_count):
        if pos + 4 > len(data):
            break
        length = struct.unpack_from("<i", data, pos)[0]
        text_start = pos + 4
        if length > 0:
            raw = data[text_start:text_start + length]
            text = raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")
            pos = text_start + length
        elif length < 0:
            byte_count = -length * 2
            raw = data[text_start:text_start + byte_count]
            text = raw.split(b"\x00\x00", 1)[0].decode("utf-16-le", errors="replace")
            pos = text_start + byte_count
        else:
            text = ""
            pos = text_start
        pos += 8
        entries.append((text_start, length, text))
    return entries


def _apply_header_renames(data: bytearray, name_offset: int, entries: List[Tuple[int, int, str]], pairs: Sequence[Tuple[str, str]]) -> List[str]:
    log: List[str] = []
    for i, (text_start, length, old_text) in enumerate(entries):
        cleaned = clean_name(old_text)
        for old, new in pairs:
            if cleaned == old:
                if length > 0:
                    new_enc = new.encode("ascii") + b"\x00"
                    if len(new_enc) <= length:
                        data[text_start:text_start + len(new_enc)] = new_enc
                        pad = length - len(new_enc)
                        if pad:
                            data[text_start + len(new_enc):text_start + length] = b"\x00" * pad
                        log.append(f"RENAMED: name[{i}] {old!r} -> {new!r}")
                elif length < 0:
                    new_enc = new.encode("utf-16-le") + b"\x00\x00"
                    char_cap = -length
                    if len(new) + 1 <= char_cap:
                        data[text_start:text_start + len(new_enc)] = new_enc
                        pad_chars = char_cap - len(new) - 1
                        if pad_chars:
                            data[text_start + len(new_enc):text_start + (-length) * 2] = b"\x00\x00" * pad_chars
                        log.append(f"RENAMED: name[{i}] {old!r} -> {new!r}")
                break
    return log


def swap_one_package(upk, donor_path: Path, output_path: Path, target_key_path: Path, pairs: Sequence[Tuple[str, str]], options: SwapOptions) -> Tuple[Path, List[str]]:
    log: List[str] = []

    if not donor_path.exists():
        raise FileNotFoundError(f"Donor package not found: {donor_path}")
    if output_path.exists() and not options.overwrite:
        raise FileExistsError(f"Output already exists: {output_path}")

    log.append(f"Donor:           {donor_path}")
    log.append(f"Output:          {output_path}")
    log.append(f"Key source:      {target_key_path}")

    if not options.keys_path or not options.keys_path.exists():
        raise FileNotFoundError(f"keys.txt not found: {options.keys_path}")

    provider = upk.DecryptionProvider(str(options.keys_path))
    summary, meta, encrypted_data, donor_key = upk.find_valid_key(
        donor_path, provider
    )

    name_offset = summary.name_offset
    name_count = summary.name_count
    depends_offset = summary.depends_offset

    log.append(f"Key found:       {base64.b64encode(donor_key).decode()}")
    log.append(f"Name count:      {name_count}")
    log.append(f"Name offset:     {name_offset}")
    log.append(f"Depends offset:  {depends_offset}")

    header_byte_count = depends_offset - name_offset
    decrypt_len = ((header_byte_count + 31) & ~15) + 32
    if decrypt_len > len(encrypted_data):
        decrypt_len = len(encrypted_data)
    encrypted_header = encrypted_data[:decrypt_len]
    header_decrypted = bytearray(upk.DecryptionProvider.decrypt_ecb(donor_key, encrypted_header))

    entries = _walk_name_table(bytes(header_decrypted), 0, name_count)
    rename_log = _apply_header_renames(header_decrypted, 0, entries, pairs)
    log.extend(rename_log)

    try:
        target_key = upk.find_key_for_encrypted_upk(target_key_path, provider)
        log.append(f"Target key:      {base64.b64encode(target_key).decode()}")
    except Exception:
        log.append("WARN: target key not found, reusing donor key")
        target_key = donor_key

    re_encrypted = upk.DecryptionProvider.encrypt_ecb(target_key, bytes(header_decrypted))

    donor_bytes = donor_path.read_bytes()
    output = bytearray(donor_bytes)
    output[name_offset:name_offset + len(re_encrypted)] = re_encrypted

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        backup_path = output_path.with_suffix(output_path.suffix + ".bak")
        shutil.copy2(output_path, backup_path)
        log.append(f"Backup:          {backup_path}")
    output_path.write_bytes(output)
    log.append(f"Written:         {output_path}")
    return output_path, log


def swap_asset(upk, target: Item, donor: Item, options: SwapOptions) -> Tuple[List[Path], List[str]]:
    if target.slot != donor.slot:
        raise ValueError(f"Slot mismatch: target={target.slot!r}, donor={donor.slot!r}")
    key_dir = options.key_source_dir or options.donor_dir
    all_paths: List[Path] = []
    all_log: List[str] = []
    all_log.append(f"Target/replaced item: {target.label}")
    all_log.append(f"Donor/visual item:    {donor.label}")
    main_path, main_log = swap_one_package(
        upk,
        options.donor_dir / donor.asset_package,
        options.output_dir / target.asset_package,
        key_dir / target.asset_package,
        infer_name_pairs(target, donor),
        options,
    )
    all_paths.append(main_path)
    all_log.extend(main_log)

    if options.include_thumbnails:
        donor_thumb = options.donor_dir / donor.thumbnail_package
        target_thumb = options.output_dir / target.thumbnail_package
        key_thumb = key_dir / target.thumbnail_package
        if donor_thumb.exists() and key_thumb.exists():
            all_log.append("")
            all_log.append("Thumbnail/_T_SF pass:")
            thumb_path, thumb_log = swap_one_package(upk, donor_thumb, target_thumb, key_thumb, infer_thumbnail_pairs(target, donor), options)
            all_paths.append(thumb_path)
            all_log.extend(thumb_log)
        else:
            all_log.append(f"SKIP thumbnails: missing {donor_thumb if not donor_thumb.exists() else key_thumb}")
    else:
        all_log.append("SKIP thumbnails: disabled.")

    return all_paths, all_log


def revert_item(target: Item, options: SwapOptions) -> Tuple[List[Path], List[str]]:
    src_dir = options.key_source_dir or options.donor_dir
    paths: List[Path] = []
    log: List[str] = []
    pairs = [(src_dir / target.asset_package, options.output_dir / target.asset_package)]
    if options.include_thumbnails:
        pairs.append((src_dir / target.thumbnail_package, options.output_dir / target.thumbnail_package))
    for src, dst in pairs:
        if not src.exists():
            log.append(f"MISS: revert source not found: {src}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and options.overwrite:
            backup_path = dst.with_suffix(dst.suffix + ".bak")
            shutil.copy2(dst, backup_path)
            log.append(f"Backup written: {backup_path}")
        shutil.copy2(src, dst)
        paths.append(dst)
        log.append(f"Reverted: {src} -> {dst}")
    return paths, log


class AssetSwapperApp:
    def __init__(self, root: tk.Tk, args: Optional[argparse.Namespace] = None):
        self.root = root
        self.root.title("RL Asset Swapper")
        self.root.geometry("1200x800")
        self.upk = import_rl_upk_editor()
        args = args or argparse.Namespace()

        self.items_path = tk.StringVar(value=str(getattr(args, "items", None) or default_path(("items.json", "items(4).json"))))
        self.keys_path = tk.StringVar(value=str(getattr(args, "keys", None) or default_path(("keys.txt", "keys(1).txt"))))
        self.donor_dir = tk.StringVar(value=str(getattr(args, "donor_dir", "") or ""))
        self.out_dir = tk.StringVar(value=str(getattr(args, "output_dir", "") or ""))
        self.key_source_dir = tk.StringVar(value=str(getattr(args, "key_source_dir", "") or ""))
        self.slot_var = tk.StringVar(value=str(getattr(args, "slot", "") or ""))
        self.target_search = tk.StringVar(value=str(getattr(args, "target", "") or ""))
        self.donor_search = tk.StringVar(value=str(getattr(args, "donor", "") or ""))
        self.overwrite_var = tk.BooleanVar(value=bool(getattr(args, "overwrite", True)))
        self.thumbnails_var = tk.BooleanVar(value=bool(getattr(args, "include_thumbnails", False)))
        self.preserve_offsets_var = tk.BooleanVar(value=bool(getattr(args, "preserve_header_offsets", True)))
        self.status_var = tk.StringVar(value="Load items.json, select folders, choose slot, then choose target and donor items.")

        self.items: List[Item] = []
        self.target_items: List[Item] = []
        self.donor_items: List[Item] = []
        self.worker_queue: queue.Queue = queue.Queue()
        self.slot_values: List[str] = []

        self.build_ui()
        if Path(self.items_path.get()).exists():
            self.reload_items()
        self.root.after(100, self.poll_worker_queue)

    def build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        files = ttk.LabelFrame(main, text="Files")
        files.pack(fill="x")
        files.columnconfigure(1, weight=1)
        files.columnconfigure(4, weight=1)

        ttk.Label(files, text="items.json").grid(row=0, column=0, sticky="w", padx=4, pady=3)
        ttk.Entry(files, textvariable=self.items_path).grid(row=0, column=1, sticky="ew", padx=4, pady=3)
        ttk.Button(files, text="Browse", command=self.browse_items).grid(row=0, column=2, padx=4, pady=3)
        ttk.Button(files, text="Reload", command=self.reload_items).grid(row=0, column=3, padx=4, pady=3)
        ttk.Label(files, text="keys.txt").grid(row=0, column=4, sticky="e", padx=4, pady=3)
        ttk.Entry(files, textvariable=self.keys_path, width=35).grid(row=0, column=5, sticky="ew", padx=4, pady=3)
        ttk.Button(files, text="Browse", command=self.browse_keys).grid(row=0, column=6, padx=4, pady=3)

        ttk.Label(files, text="Donor/input directory").grid(row=1, column=0, sticky="w", padx=4, pady=3)
        ttk.Entry(files, textvariable=self.donor_dir).grid(row=1, column=1, columnspan=5, sticky="ew", padx=4, pady=3)
        ttk.Button(files, text="Browse", command=self.browse_donor_dir).grid(row=1, column=6, padx=4, pady=3)

        ttk.Label(files, text="Output directory").grid(row=2, column=0, sticky="w", padx=4, pady=3)
        ttk.Entry(files, textvariable=self.out_dir).grid(row=2, column=1, columnspan=5, sticky="ew", padx=4, pady=3)
        ttk.Button(files, text="Browse", command=self.browse_out_dir).grid(row=2, column=6, padx=4, pady=3)

        ttk.Label(files, text="Key/revert source dir").grid(row=3, column=0, sticky="w", padx=4, pady=3)
        ttk.Entry(files, textvariable=self.key_source_dir).grid(row=3, column=1, columnspan=5, sticky="ew", padx=4, pady=3)
        ttk.Button(files, text="Browse", command=self.browse_key_source_dir).grid(row=3, column=6, padx=4, pady=3)

        top = ttk.Frame(main)
        top.pack(fill="x", pady=(8, 4))
        ttk.Label(top, text="Slot").pack(side="left")
        self.slot_combo = ttk.Combobox(top, textvariable=self.slot_var, state="readonly", width=36)
        self.slot_combo.pack(side="left", padx=(6, 12))
        self.slot_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_lists(clear_selection=True))
        ttk.Checkbutton(top, text="Also swap thumbnails/_T_SF", variable=self.thumbnails_var, command=self.update_preview).pack(side="left", padx=4)
        ttk.Checkbutton(top, text="Preserve header offsets for shorter names", variable=self.preserve_offsets_var, command=self.update_preview).pack(side="left", padx=4)
        ttk.Checkbutton(top, text="Overwrite + .bak", variable=self.overwrite_var).pack(side="left", padx=4)
        ttk.Button(top, text="Revert selected target", command=self.start_revert).pack(side="right", padx=4)
        ttk.Button(top, text="Swap", command=self.start_swap).pack(side="right", padx=4)

        lists = ttk.Frame(main)
        lists.pack(fill="both", expand=True)
        lists.columnconfigure(0, weight=1)
        lists.columnconfigure(1, weight=1)
        lists.rowconfigure(2, weight=1)

        ttk.Label(lists, text="Target item to replace").grid(row=0, column=0, sticky="w")
        ttk.Label(lists, text="Replacement/donor item").grid(row=0, column=1, sticky="w")

        ttk.Entry(lists, textvariable=self.target_search).grid(row=1, column=0, sticky="ew", padx=(0, 5), pady=(0, 4))
        ttk.Entry(lists, textvariable=self.donor_search).grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=(0, 4))
        self.target_search.trace_add("write", lambda *_: self.refresh_target_list())
        self.donor_search.trace_add("write", lambda *_: self.refresh_donor_list())

        left = ttk.Frame(lists)
        right = ttk.Frame(lists)
        left.grid(row=2, column=0, sticky="nsew", padx=(0, 5))
        right.grid(row=2, column=1, sticky="nsew", padx=(5, 0))
        for frame in (left, right):
            frame.rowconfigure(0, weight=1)
            frame.columnconfigure(0, weight=1)

        self.target_list = tk.Listbox(left, activestyle="dotbox", exportselection=False)
        self.target_list.grid(row=0, column=0, sticky="nsew")
        target_scroll = ttk.Scrollbar(left, orient="vertical", command=self.target_list.yview)
        target_scroll.grid(row=0, column=1, sticky="ns")
        self.target_list.configure(yscrollcommand=target_scroll.set)
        self.target_list.bind("<<ListboxSelect>>", lambda _e: self.update_preview())

        self.donor_list = tk.Listbox(right, activestyle="dotbox", exportselection=False)
        self.donor_list.grid(row=0, column=0, sticky="nsew")
        donor_scroll = ttk.Scrollbar(right, orient="vertical", command=self.donor_list.yview)
        donor_scroll.grid(row=0, column=1, sticky="ns")
        self.donor_list.configure(yscrollcommand=donor_scroll.set)
        self.donor_list.bind("<<ListboxSelect>>", lambda _e: self.update_preview())

        bottom = ttk.PanedWindow(main, orient="vertical")
        bottom.pack(fill="both", expand=False, pady=(8, 0))

        preview_frame = ttk.LabelFrame(bottom, text="Preview")
        self.preview = tk.Text(preview_frame, height=7, wrap="none")
        self.preview.pack(fill="both", expand=True)
        bottom.add(preview_frame, weight=1)

        log_frame = ttk.LabelFrame(bottom, text="Log")
        self.log = tk.Text(log_frame, height=10, wrap="none")
        self.log.pack(fill="both", expand=True)
        bottom.add(log_frame, weight=1)

        ttk.Label(main, textvariable=self.status_var, anchor="w").pack(fill="x", pady=(4, 0))

    def browse_items(self) -> None:
        path = filedialog.askopenfilename(title="Select items.json", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if path:
            self.items_path.set(path)
            self.reload_items()

    def browse_keys(self) -> None:
        path = filedialog.askopenfilename(title="Select keys.txt", filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if path:
            self.keys_path.set(path)

    def browse_donor_dir(self) -> None:
        path = filedialog.askdirectory(title="Select donor/input UPK directory")
        if path:
            self.donor_dir.set(path)
            if not self.out_dir.get():
                self.out_dir.set(path)
            if not self.key_source_dir.get():
                self.key_source_dir.set(path)

    def browse_out_dir(self) -> None:
        path = filedialog.askdirectory(title="Select output directory")
        if path:
            self.out_dir.set(path)

    def browse_key_source_dir(self) -> None:
        path = filedialog.askdirectory(title="Select key/revert source directory")
        if path:
            self.key_source_dir.set(path)

    def reload_items(self) -> None:
        try:
            self.items = load_items(Path(self.items_path.get()))
            slots = sorted({i.slot for i in self.items if i.slot})
            self.slot_values = slots
            self.slot_combo["values"] = slots
            if slots and self.slot_var.get() not in slots:
                self.slot_var.set(slots[0])
            self.refresh_lists(clear_selection=True)
            self.status_var.set(f"Loaded {len(self.items)} items. Slot filter is active.")
        except Exception as exc:
            messagebox.showerror("Failed to load items", str(exc))

    def rows_for(self, text: str) -> List[Item]:
        slot = self.slot_var.get()
        q = text.strip().lower()
        rows = [i for i in self.items if i.slot == slot] if slot else list(self.items)
        if q:
            rows = [
                i for i in rows
                if q in i.product.lower()
                or q in i.asset_package.lower()
                or q in i.asset_path.lower()
                or q == str(i.id)
            ]
        return rows

    def refresh_lists(self, clear_selection: bool = False) -> None:
        self.refresh_target_list(clear_selection=clear_selection)
        self.refresh_donor_list(clear_selection=clear_selection)
        self.update_preview()

    def refresh_target_list(self, clear_selection: bool = False) -> None:
        old_id = self.selected_target().id if self.selected_target() and not clear_selection else None
        self.target_items = self.rows_for(self.target_search.get())
        self.target_list.delete(0, tk.END)
        restore = None
        for idx, item in enumerate(self.target_items):
            self.target_list.insert(tk.END, item.label)
            if old_id is not None and item.id == old_id:
                restore = idx
        if restore is not None:
            self.target_list.selection_set(restore)
            self.target_list.see(restore)

    def refresh_donor_list(self, clear_selection: bool = False) -> None:
        old_id = self.selected_donor().id if self.selected_donor() and not clear_selection else None
        self.donor_items = self.rows_for(self.donor_search.get())
        self.donor_list.delete(0, tk.END)
        restore = None
        for idx, item in enumerate(self.donor_items):
            self.donor_list.insert(tk.END, item.label)
            if old_id is not None and item.id == old_id:
                restore = idx
        if restore is not None:
            self.donor_list.selection_set(restore)
            self.donor_list.see(restore)

    def selected_target(self) -> Optional[Item]:
        sel = self.target_list.curselection()
        return self.target_items[sel[0]] if sel else None

    def selected_donor(self) -> Optional[Item]:
        sel = self.donor_list.curselection()
        return self.donor_items[sel[0]] if sel else None

    def update_preview(self) -> None:
        target = self.selected_target()
        donor = self.selected_donor()
        self.preview.delete("1.0", tk.END)
        if not target or not donor:
            slot = self.slot_var.get() or "<none>"
            self.preview.insert(tk.END, f"Slot filter: {slot}\nSelect a target item and a donor item.\n")
            return
        lines = [
            f"Slot filter: {self.slot_var.get()}",
            f"Output file: {target.asset_package}",
            f"Input file:  {donor.asset_package}",
            f"Preserve shorter-name offsets: {self.preserve_offsets_var.get()}",
            "",
            "Main package replacements:",
        ]
        for old, new in infer_name_pairs(target, donor):
            lines.append(f"  {old!r} -> {new!r}")
        if self.thumbnails_var.get():
            lines.append("")
            lines.append(f"Thumbnail file: {donor.thumbnail_package} -> {target.thumbnail_package}")
            for old, new in infer_thumbnail_pairs(target, donor):
                lines.append(f"  {old!r} -> {new!r}")
        self.preview.insert(tk.END, "\n".join(lines) + "\n")

    def make_options(self) -> SwapOptions:
        if not self.donor_dir.get():
            raise ValueError("Select donor/input directory")
        if not self.out_dir.get():
            raise ValueError("Select output directory")
        keys = Path(self.keys_path.get()) if self.keys_path.get() else None
        if keys and not keys.exists():
            keys = None
        key_source = Path(self.key_source_dir.get()) if self.key_source_dir.get() else None
        return SwapOptions(
            items_path=Path(self.items_path.get()),
            keys_path=keys,
            donor_dir=Path(self.donor_dir.get()),
            output_dir=Path(self.out_dir.get()),
            key_source_dir=key_source,
            include_thumbnails=self.thumbnails_var.get(),
            preserve_header_offsets=self.preserve_offsets_var.get(),
            overwrite=self.overwrite_var.get(),
        )

    def append_log(self, text: str) -> None:
        self.log.insert(tk.END, text.rstrip() + "\n")
        self.log.see(tk.END)

    def start_swap(self) -> None:
        target = self.selected_target()
        donor = self.selected_donor()
        if not target or not donor:
            messagebox.showwarning("Missing selection", "Select both a target item and a donor item.")
            return
        if target.slot != donor.slot:
            messagebox.showerror("Slot mismatch", "Target and donor items must be from the same slot.")
            return
        try:
            options = self.make_options()
        except Exception as exc:
            messagebox.showwarning("Missing input", str(exc))
            return
        self.log.delete("1.0", tk.END)
        self.status_var.set("Working...")
        threading.Thread(target=self.worker_swap, args=(target, donor, options), daemon=True).start()

    def start_revert(self) -> None:
        target = self.selected_target()
        if not target:
            messagebox.showwarning("Missing selection", "Select the target item to revert.")
            return
        try:
            options = self.make_options()
        except Exception as exc:
            messagebox.showwarning("Missing input", str(exc))
            return
        self.log.delete("1.0", tk.END)
        self.status_var.set("Reverting...")
        threading.Thread(target=self.worker_revert, args=(target, options), daemon=True).start()

    def worker_swap(self, target: Item, donor: Item, options: SwapOptions) -> None:
        try:
            paths, log = swap_asset(self.upk, target, donor, options)
            self.worker_queue.put(("ok", paths, log))
        except Exception as exc:
            self.worker_queue.put(("err", str(exc), traceback.format_exc()))

    def worker_revert(self, target: Item, options: SwapOptions) -> None:
        try:
            paths, log = revert_item(target, options)
            self.worker_queue.put(("ok", paths, log))
        except Exception as exc:
            self.worker_queue.put(("err", str(exc), traceback.format_exc()))

    def poll_worker_queue(self) -> None:
        try:
            while True:
                kind, a, b = self.worker_queue.get_nowait()
                if kind == "ok":
                    for line in b:
                        self.append_log(line)
                    self.status_var.set("Done: " + ", ".join(str(x) for x in a))
                    messagebox.showinfo("Complete", "Saved:\n" + "\n".join(str(x) for x in a))
                else:
                    self.append_log(b)
                    self.status_var.set("Failed")
                    messagebox.showerror("Failed", a)
        except queue.Empty:
            pass
        self.root.after(100, self.poll_worker_queue)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--items", type=Path, default=default_path(("items.json", "items(4).json")))
    p.add_argument("--keys", type=Path, default=None)
    p.add_argument("--donor-dir", "--upk-dir", "--input-dir", dest="donor_dir", type=Path, default=None)
    p.add_argument("--output-dir", "--out-dir", dest="output_dir", type=Path, default=None)
    p.add_argument("--key-source-dir", type=Path, default=None)
    p.add_argument("--slot", default="")
    p.add_argument("--target", default="")
    p.add_argument("--donor", default="")
    p.add_argument("--auto-swap", action="store_true")
    p.add_argument("--no-gui", action="store_true")
    p.add_argument("--revert", action="store_true")
    thumbs = p.add_mutually_exclusive_group()
    thumbs.add_argument("--include-thumbnails", dest="include_thumbnails", action="store_true", default=False)
    thumbs.add_argument("--no-thumbnails", dest="include_thumbnails", action="store_false")
    preserve = p.add_mutually_exclusive_group()
    preserve.add_argument("--preserve-header-offsets", dest="preserve_header_offsets", action="store_true", default=True)
    preserve.add_argument("--no-preserve-header-offsets", dest="preserve_header_offsets", action="store_false")
    overwrite = p.add_mutually_exclusive_group()
    overwrite.add_argument("--overwrite", dest="overwrite", action="store_true", default=True)
    overwrite.add_argument("--no-overwrite", dest="overwrite", action="store_false")
    return p


def cli_run(args: argparse.Namespace) -> int:
    if not args.donor_dir or not args.output_dir:
        raise SystemExit("--donor-dir and --output-dir are required for --no-gui/--auto-swap/--revert")
    if args.revert and not args.target:
        raise SystemExit("--target is required for --revert")
    if not args.revert and (not args.target or not args.donor):
        raise SystemExit("--target and --donor are required")
    upk = import_rl_upk_editor()
    items = load_items(args.items)
    target = find_item(items, str(args.target), args.slot)
    donor = find_item(items, str(args.donor), target.slot if not args.slot else args.slot) if args.donor else target
    keys = args.keys
    if keys is None:
        for candidate in (script_dir() / "keys.txt", script_dir() / "keys(1).txt", Path.cwd() / "keys.txt", args.donor_dir / "keys.txt"):
            if candidate.exists():
                keys = candidate
                break
    options = SwapOptions(
        items_path=args.items,
        keys_path=keys,
        donor_dir=args.donor_dir,
        output_dir=args.output_dir,
        key_source_dir=args.key_source_dir,
        include_thumbnails=args.include_thumbnails,
        preserve_header_offsets=args.preserve_header_offsets,
        overwrite=args.overwrite,
    )
    if args.revert:
        _, log = revert_item(target, options)
    else:
        _, log = swap_asset(upk, target, donor, options)
    for line in log:
        print(line)
    return 0


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.no_gui or args.auto_swap or args.revert:
        return cli_run(args)
    root = tk.Tk()
    AssetSwapperApp(root, args)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())