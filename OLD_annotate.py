#!/usr/bin/env python3
"""Annotation tool for wheatpea plant counting.

Usage:
    python wheatpea_count/annotate.py

Controls:
    Mode Écrire + Point     : clic → place un point
    Mode Écrire + Rectangle : clic-1 ancre, clic-2 finalise la boîte
    Mode Effacer + Point     : clic → supprime le point le plus proche
    Mode Effacer + Rectangle : clic → supprime la boîte la plus proche (par son centre)

Catégorie:
    Pea   → points rouges uniquement
    Wheat → points bleu foncé uniquement
    Mixed → deux espèces ; sélection active via les boutons Pois / Blé

Pour le blé, annoter la base / couronne de chaque plante.
Pour le pois, annoter le centre de chaque plante.

Sauvegarde au format FSC-147 dans wheatpea_count/annotations/annotation.json.
Images mixed → deux entrées : <stem>_pea<ext> (mixed_pea) et <stem>_wheat<ext> (mixed_wheat).
Split train/val/test : défini par parcelle (plot_id = x<c>y<r>), verrouillé dès la première tuile sauvegardée.
Toutes les tuiles d'une même parcelle héritent automatiquement du même split.
"""

import argparse
import csv
import json
import math
import re
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, simpledialog
from pathlib import Path
from typing import Optional

from PIL import Image, ImageTk

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE      = Path(__file__).resolve().parent
IMG_DIR    = _HERE / "annotations" / "images"
ANN_FILE   = _HERE / "annotations" / "annotation.json"
CLEAN_CSV  = _HERE.parent / "data" / "sticsmix_density_clean.csv"
SPLIT_FILE   = _HERE / "split.json"
COMPARE_CSV    = _HERE / "linm_compare.csv"
COMPARE_SCRIPT = _HERE / "compare_linm.py"
LINM_REF       = _HERE.parent / "drone" / "extract_linm" / "linm_ref.json"
RAW_CSV        = _HERE.parent / "data" / "sticsmix_plant-density_data-ok[45].csv"

# ── Appearance ────────────────────────────────────────────────────────────────
COLOR_PEA   = "#FF4444"
COLOR_WHEAT = "#1A237E"
POINT_R     = 5
BOX_W       = 2
PANEL_W     = 345
_MAX_ZOOM   = 10.0


# ── FSC-147 helpers ───────────────────────────────────────────────────────────

def xyxy_to_fsc(x1: float, y1: float, x2: float, y2: float) -> list:
    """Convert xyxy to FSC-147 4-corner list [[x1,y1],[x1,y2],[x2,y2],[x2,y1]]."""
    return [[x1, y1], [x1, y2], [x2, y2], [x2, y1]]


def fsc_to_xyxy(corners: list) -> list[float]:
    """Convert FSC-147 corners to [x1, y1, x2, y2]."""
    return [corners[0][0], corners[0][1], corners[2][0], corners[2][1]]


def box_center(box: list) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def dist2(ax: float, ay: float, bx: float, by: float) -> float:
    return (ax - bx) ** 2 + (ay - by) ** 2


# ── Main annotator ────────────────────────────────────────────────────────────

class Annotator:
    def __init__(self, root: tk.Tk, images: list[Path]) -> None:
        self.root   = root
        self.images = images
        self.idx    = 0

        self.annotations: dict = {}
        if ANN_FILE.exists():
            with open(ANN_FILE) as f:
                self.annotations = json.load(f)

        # parcelle → type from sticsmix_density_clean.csv (used for auto-inference)
        self.csv_types: dict[str, str] = {}
        if CLEAN_CSV.exists():
            with open(CLEAN_CSV, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    self.csv_types[row["parcelle"]] = row["type"]

        # stem → comparison row from linm_compare.csv (snapshot du dernier run)
        self.compare_data: dict[str, dict] = {}
        if COMPARE_CSV.exists():
            with open(COMPARE_CSV, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    angle_val = row.get("angle", "")
                    suffix = f"_{angle_val}" if angle_val else ""
                    key = f"{row['parcelle']}_{row['metre_lineaire']}{suffix}"
                    self.compare_data[key] = row

        # coins L93 par transect pour calculer la résolution réelle en mm/px
        self.linm_corners: dict = {}
        if LINM_REF.exists():
            self.linm_corners = json.loads(LINM_REF.read_text()).get("linmeters", {})

        # stem → [seq_0cm, seq_25cm, seq_50cm, seq_75cm] depuis le CSV brut
        self.raw_seqs: dict[str, list[str]] = {}
        if RAW_CSV.exists():
            with open(RAW_CSV, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f, delimiter="\t"):
                    stem = f"{row['x']}_{row['y']}_{row['linmeter']}"
                    self.raw_seqs[stem] = [
                        row["North.0.25"],
                        row["X25.50"],
                        row["X50.75"],
                        row["X75.100.South"],
                    ]

        self.pixel_mm: float = 0.7  # mis à jour à chaque chargement d'image

        # parcelle → split from split.json
        self.plot_splits: dict[str, str] = {}
        if SPLIT_FILE.exists():
            with open(SPLIT_FILE) as f:
                data = json.load(f)
            for split, parcelles in data.items():
                for p in parcelles:
                    self.plot_splits[p] = split

        # per-image state
        self.category: str = "pea"   # pea | wheat | mixed
        self.species:  str = "pea"   # active species (pea | wheat)
        self.mode:     str = "write" # write | erase
        self.tool:     str = "point" # point | rect

        # annotations in image coordinates
        self.pea_points:   list[list[float]] = []
        self.wheat_points: list[list[float]] = []
        self.pea_boxes:    list[list[float]] = []  # each: [x1,y1,x2,y2]
        self.wheat_boxes:  list[list[float]] = []

        # split & category assignment (per plot, locked after first save)
        self.plot_id:        str           = ""
        self.split:          Optional[str] = None   # "train" | "val" | "test"
        self.split_locked:   bool          = False
        self.category_locked: bool         = False

        # in-progress rectangle
        self.rect_anchor:  Optional[tuple[int, int]] = None
        self.rect_prev_id: Optional[int]             = None

        self.scale:  float               = 1.0
        self.img_w:  int                 = 1
        self.img_h:  int                 = 1
        self.pil_img: Optional[Image.Image] = None
        self._photo  = None

        # zoom & pan (reset on each image load)
        self.zoom:        float                     = 1.0
        self.pan_x:       float                     = 0.0
        self.pan_y:       float                     = 0.0
        self._pan_anchor: Optional[tuple[int, int]] = None

        self._dirty: bool = False  # unsaved edits on current image
        self._stats_target: str = "main"  # "main" | "companion"

        # companion image (opposite angle, editable)
        self.companion_pil:          Optional[Image.Image] = None
        self._companion_photo                              = None
        self.companion_pea_points:   list[list[float]]    = []
        self.companion_wheat_points: list[list[float]]    = []
        self.companion_pea_boxes:    list[list[float]]    = []
        self.companion_wheat_boxes:  list[list[float]]    = []
        self.companion_name:         str                  = ""
        self.companion_rect_anchor:  Optional[tuple[int, int]] = None
        self.companion_rect_prev_id: Optional[int]             = None

        self._build_ui()
        self._load_image()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.root.title("Annotateur wheatpea")
        self.root.configure(bg="#1a1a1a")

        # frame containing both image canvases side by side
        img_frame = tk.Frame(self.root, bg="#1a1a1a")
        img_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # companion half (left) — each half gets equal space via expand=True
        companion_frame = tk.Frame(img_frame, bg="#111111")
        companion_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.companion_canvas = tk.Canvas(companion_frame, bg="#111111", cursor="crosshair",
                                          highlightthickness=0)
        self.companion_canvas.pack(fill=tk.BOTH, expand=True)
        self.companion_canvas.bind("<MouseWheel>", self._on_wheel)
        try:
            self.companion_canvas.bind("<Magnify>", self._on_magnify)
        except Exception:
            pass
        self.companion_canvas.bind("<Button-1>", self._on_companion_click)
        self.companion_canvas.bind("<Motion>",   self._on_companion_motion)
        for _b in (2, 3):
            self.companion_canvas.bind(f"<ButtonPress-{_b}>",   self._on_pan_start)
            self.companion_canvas.bind(f"<B{_b}-Motion>",        self._on_pan_drag)
            self.companion_canvas.bind(f"<ButtonRelease-{_b}>",  self._on_pan_end)

        # editable half (right)
        edit_frame = tk.Frame(img_frame, bg="#0d0d0d")
        edit_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(edit_frame, bg="#0d0d0d", cursor="crosshair",
                                highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Button-1>",        self._on_click)
        self.canvas.bind("<Motion>",          self._on_motion)
        self.canvas.bind("<MouseWheel>",      self._on_wheel)
        try:
            self.canvas.bind("<Magnify>", self._on_magnify)
        except Exception:
            pass  # not available on all Tk builds
        # Button-2 = right-click on macOS trackpad; Button-3 = real right button
        for _b in (2, 3):
            self.canvas.bind(f"<ButtonPress-{_b}>",   self._on_pan_start)
            self.canvas.bind(f"<B{_b}-Motion>",        self._on_pan_drag)
            self.canvas.bind(f"<ButtonRelease-{_b}>",  self._on_pan_end)

        panel = tk.Frame(self.root, bg="#1a1a1a", width=PANEL_W)
        panel.pack(side=tk.RIGHT, fill=tk.Y, padx=6, pady=6)
        panel.pack_propagate(False)

        def lbl(text="", size=10, fg="#cccccc", **kw) -> tk.Label:
            return tk.Label(panel, text=text, bg="#1a1a1a", fg=fg,
                            font=("Helvetica", size), **kw)

        def sep() -> None:
            tk.Frame(panel, bg="#3a3a3a", height=1).pack(fill=tk.X, pady=5)

        def row_frame() -> tk.Frame:
            f = tk.Frame(panel, bg="#1a1a1a")
            f.pack(fill=tk.X, pady=(2, 4))
            return f

        def _navlbl(text: str, bg: str, cmd) -> tk.Label:
            lb = tk.Label(panel, text=text, font=("Helvetica", 10, "bold"),
                          bg=bg, fg="white", pady=7, cursor="hand2", relief=tk.FLAT)
            lb.bind("<Button-1>", lambda _e: cmd())
            return lb

        # image info
        self.lbl_file = lbl(size=9, fg="#aaaaaa", anchor="w",
                            wraplength=PANEL_W - 10, justify=tk.LEFT)
        self.lbl_file.pack(fill=tk.X)
        self.lbl_idx = lbl(size=9, fg="#666666", anchor="w")
        self.lbl_idx.pack(fill=tk.X)
        sep()

        # row 1 — mode
        lbl("Mode", size=9, fg="#777777").pack(anchor="w")
        r1 = row_frame()
        self.btn_write = self._tbtn(r1, "Écrire",  lambda: self._set_mode("write"))
        self.btn_erase = self._tbtn(r1, "Effacer", lambda: self._set_mode("erase"))
        self.btn_write.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        self.btn_erase.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # row 2 — tool
        lbl("Outil", size=9, fg="#777777").pack(anchor="w")
        r2 = row_frame()
        self.btn_point = self._tbtn(r2, "Point",     lambda: self._set_tool("point"))
        self.btn_rect  = self._tbtn(r2, "Rectangle", lambda: self._set_tool("rect"))
        self.btn_point.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        self.btn_rect.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # row 3 — category
        lbl("Catégorie", size=9, fg="#777777").pack(anchor="w")
        r3 = row_frame()
        self.btn_cat_pea   = self._tbtn(r3, "Pea",   lambda: self._set_category("pea"))
        self.btn_cat_wheat = self._tbtn(r3, "Wheat", lambda: self._set_category("wheat"))
        self.btn_cat_mixed = self._tbtn(r3, "Mixed", lambda: self._set_category("mixed"))
        self.btn_cat_pea.pack(  side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 1))
        self.btn_cat_wheat.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 1))
        self.btn_cat_mixed.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # row 4 — active species (always visible; disabled for pure images)
        lbl("Espèce active", size=9, fg="#777777").pack(anchor="w")
        r4 = row_frame()
        self.btn_sp_pea   = self._tbtn(r4, "Pois", lambda: self._set_species("pea"))
        self.btn_sp_wheat = self._tbtn(r4, "Blé",  lambda: self._set_species("wheat"))
        self.btn_sp_pea.pack(  side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        self.btn_sp_wheat.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # row 5 — split (train / val / test) — verrouillé après première sauvegarde
        lbl("Split parcelle", size=9, fg="#777777").pack(anchor="w")
        r5 = row_frame()
        self.btn_train = self._tbtn(r5, "Train", lambda: self._set_split("train"))
        self.btn_val   = self._tbtn(r5, "Val",   lambda: self._set_split("val"))
        self.btn_test  = self._tbtn(r5, "Test",  lambda: self._set_split("test"))
        self.btn_train.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 1))
        self.btn_val.pack(  side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 1))
        self.btn_test.pack( side=tk.LEFT, fill=tk.X, expand=True)

        sep()

        # toggle stats principal / compagnon
        self.btn_stats_target = _navlbl("Stats compagnon", "#2a3a4a", self._toggle_stats_target)
        self.btn_stats_target.pack(fill=tk.X, pady=(0, 3))

        # stats
        self.lbl_stats = lbl(size=9, fg="#aaaaaa", justify=tk.LEFT, anchor="w")
        self.lbl_stats.pack(fill=tk.X)

        sep()

        # navigation
        self.btn_prev  = _navlbl("◀ Précédent",         "#2a2a6a", self._go_prev)
        self.btn_reset = _navlbl("⟳ Réinitialiser",    "#6a2a2a", self._reset)
        self.btn_save  = _navlbl("💾 Sauvegarder",     "#1a5a2a", self._save)
        self.btn_next  = _navlbl("Suivant ▶",          "#2a5a2a", self._go_next)
        self.btn_skip  = _navlbl("⤵ 1ʳᵉ non annotée", "#3a3a3a", self._go_first_unannotated)
        for b in (self.btn_prev, self.btn_reset, self.btn_save, self.btn_next, self.btn_skip):
            b.pack(fill=tk.X, pady=2)

        sep()

        # Comparison info — rempli à chaque chargement si linm_compare.csv existe
        self.lbl_compare = tk.Label(
            panel, text="", bg="#1a1a1a", fg="#aaaaaa",
            font=("Courier", 8), justify=tk.LEFT, anchor="w",
            wraplength=PANEL_W - 10,
        )
        self.lbl_compare.pack(fill=tk.X)

        sep()

        # Recherche par nom de transect
        lbl("Rechercher transect", size=9, fg="#777777").pack(anchor="w")
        sf = tk.Frame(panel, bg="#1a1a1a")
        sf.pack(fill=tk.X, pady=(2, 0))
        self.search_var = tk.StringVar()
        search_entry = tk.Entry(
            sf, textvariable=self.search_var,
            bg="#2a2a2a", fg="#cccccc", insertbackground="#cccccc",
            font=("Helvetica", 9), relief=tk.FLAT, bd=3,
        )
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        search_entry.bind("<Return>", lambda _e: self._do_search())
        btn_go = tk.Label(
            sf, text="→", font=("Helvetica", 10, "bold"),
            bg="#2a4a6a", fg="white", pady=5, padx=8,
            cursor="hand2", relief=tk.FLAT,
        )
        btn_go.bind("<Button-1>", lambda _e: self._do_search())
        btn_go.pack(side=tk.RIGHT)
        self.lbl_search_result = lbl(size=8, fg="#556666", anchor="w")
        self.lbl_search_result.pack(fill=tk.X, pady=(2, 0))
        btn_reload = _navlbl("⟳ Recharger GT", "#3a3a5a", self._reload_compare)
        btn_reload.pack(fill=tk.X, pady=(4, 0))

    def _tbtn(self, parent: tk.Frame, text: str, cmd) -> tk.Label:
        """Toggle label-button — Labels always honour bg/fg on macOS."""
        lbl = tk.Label(parent, text=text, font=("Helvetica", 9, "bold"),
                       bg="#222222", fg="#666666", pady=6, padx=2,
                       cursor="hand2", relief=tk.FLAT)
        lbl.bind("<Button-1>", lambda _e: cmd())
        return lbl

    # ── State setters ─────────────────────────────────────────────────────────

    def _set_mode(self, m: str) -> None:
        self.mode = m
        self._cancel_rect()
        self._cancel_companion_rect()
        self._refresh_panel()

    def _set_tool(self, t: str) -> None:
        self.tool = t
        self._cancel_rect()
        self._cancel_companion_rect()
        self._refresh_panel()

    def _set_category(self, c: str) -> None:
        if self.category_locked:
            return
        self.category = c
        if c == "pea":
            self.species = "pea"
        elif c == "wheat":
            self.species = "wheat"
        # mixed: keep current species selection
        self._refresh_panel()

    def _set_species(self, s: str) -> None:
        if self.category != "mixed":
            return
        self.species = s
        self._refresh_panel()

    def _set_split(self, s: str) -> None:
        if self.split_locked:
            return
        self.split = s
        self._refresh_panel()

    def _active_species(self) -> str:
        return self.species if self.category == "mixed" else self.category

    def _cancel_rect(self) -> None:
        self.rect_anchor = None
        if self.rect_prev_id is not None:
            self.canvas.delete(self.rect_prev_id)
            self.rect_prev_id = None

    def _cancel_companion_rect(self) -> None:
        self.companion_rect_anchor = None
        if self.companion_rect_prev_id is not None:
            self.companion_canvas.delete(self.companion_rect_prev_id)
            self.companion_rect_prev_id = None

    def _get_plot_split(self, plot_id: str) -> Optional[str]:
        """Return the split already assigned to this plot, or None if not yet assigned."""
        for key, ann in self.annotations.items():
            physical = ann.get("image_file", key)
            if Path(physical).stem.split("_tile_")[0] == plot_id:
                s = ann.get("split")
                if s:
                    return s
        return None

    def _get_split_file(self, plot_id: str) -> Optional[str]:
        """Look up split from split.json for this plot_id."""
        parcelle = self._parcelle_from_plot(plot_id)
        if parcelle is None:
            return None
        return self.plot_splits.get(parcelle)

    def _get_plot_category(self, plot_id: str) -> Optional[str]:
        """Return the category already assigned to this plot, normalizing mixed_* → mixed."""
        for key, ann in self.annotations.items():
            physical = ann.get("image_file", key)
            if Path(physical).stem.split("_tile_")[0] == plot_id:
                cat = ann.get("category")
                if cat:
                    return "mixed" if cat.startswith("mixed") else cat
        return None

    @staticmethod
    def _parcelle_from_plot(plot_id: str) -> Optional[str]:
        """Extract 'N_M' parcelle key from plot_id.

        Handles linm format '79_12_5SW' and old tile format 'x10y2'.
        """
        m = re.match(r'^(\d+_\d+)_\w+$', plot_id)
        if m:
            return m.group(1)
        m = re.match(r'^x(\d+)y(\d+)', plot_id)
        if m:
            return f"{m.group(1)}_{m.group(2)}"
        return None

    def _get_csv_category(self, plot_id: str) -> Optional[str]:
        """Look up category from sticsmix_density_clean.csv for this plot_id."""
        parcelle = self._parcelle_from_plot(plot_id)
        if parcelle is None:
            return None
        return self.csv_types.get(parcelle)

    def _csv_key_from_path(self, path: Path) -> str:
        """Return the linm_compare.csv lookup key (parcelle_metrelineaire) for an image path.

        Strips _pea/_wheat annotation suffixes and _tile_ tile indices.
        """
        stem = path.stem
        for suffix in ("_pea", "_wheat"):
            if stem.endswith(suffix):
                stem = stem[:-len(suffix)]
                break
        stem = stem.split("_tile_")[0]
        for angle_sfx in ("_45", "_90"):
            if stem.endswith(angle_sfx):
                stem = stem[:-len(angle_sfx)]
                break
        return stem

    @staticmethod
    def _apply_btn(btn: tk.Label, active: bool,
                   active_bg: str, active_fg: str = "white",
                   disabled: bool = False) -> None:
        """Set label-button appearance for active / inactive / disabled states."""
        if disabled:
            btn.config(bg="#1c1c1c", fg="#333333", cursor="")
        elif active:
            btn.config(bg=active_bg, fg=active_fg, cursor="hand2")
        else:
            btn.config(bg="#222222", fg="#777777", cursor="hand2")

    def _refresh_panel(self) -> None:
        ab = self._apply_btn

        ab(self.btn_write, self.mode == "write", "#2e7a2e")
        ab(self.btn_erase, self.mode == "erase", "#8a2a2a")

        ab(self.btn_point, self.tool == "point", "#2a5aaa")
        ab(self.btn_rect,  self.tool == "rect",  "#2a5aaa")

        for btn, val, col in (
            (self.btn_cat_pea,   "pea",   "#992222"),
            (self.btn_cat_wheat, "wheat", "#1a2a8a"),
            (self.btn_cat_mixed, "mixed", "#5a2a8a"),
        ):
            is_active = self.category == val
            if self.category_locked:
                btn.config(
                    bg=col if is_active else "#1e1e1e",
                    fg="#cccccc" if is_active else "#333333",
                    cursor="",
                )
            else:
                ab(btn, is_active, col)

        # species buttons: active only in mixed mode
        is_mixed = self.category == "mixed"
        ab(self.btn_sp_pea,
           active=is_mixed and self.species == "pea",
           active_bg="#cc2222",
           disabled=not is_mixed)
        ab(self.btn_sp_wheat,
           active=is_mixed and self.species == "wheat",
           active_bg="#2233aa",
           disabled=not is_mixed)

        # split buttons — verrouillés une fois la parcelle assignée
        for btn, val, col in (
            (self.btn_train, "train", "#2e6e2e"),
            (self.btn_val,   "val",   "#2a4a8a"),
            (self.btn_test,  "test",  "#7a501a"),
        ):
            is_active = self.split == val
            if self.split_locked:
                btn.config(
                    bg=col if is_active else "#1e1e1e",
                    fg="#cccccc" if is_active else "#333333",
                    cursor="",
                )
            else:
                ab(btn, is_active, col)

        if self.idx > 0:
            self.btn_prev.config(bg="#2a2a6a", fg="white",  cursor="hand2")
        else:
            self.btn_prev.config(bg="#1c1c1c", fg="#333333", cursor="")

        # stats (main ou compagnon selon le toggle)
        use_comp = self._stats_target == "companion" and self.companion_pil is not None
        if use_comp:
            np_pts = len(self.companion_pea_points)
            nw_pts = len(self.companion_wheat_points)
            np_box = len(self.companion_pea_boxes)
            nw_box = len(self.companion_wheat_boxes)
            prefix = f"[{self.companion_name}]\n"
        else:
            np_pts, nw_pts = len(self.pea_points), len(self.wheat_points)
            np_box, nw_box = len(self.pea_boxes),  len(self.wheat_boxes)
            prefix = ""
        if self.category == "mixed":
            stats = (prefix + f"Pois : {np_pts} pts · {np_box}/3 boîtes\n"
                     f"Blé  : {nw_pts} pts · {nw_box}/3 boîtes")
        elif self.category == "pea":
            stats = prefix + f"Pois : {np_pts} pts · {np_box}/3 boîtes"
        else:
            stats = prefix + f"Blé  : {nw_pts} pts · {nw_box}/3 boîtes"
        self.lbl_stats.config(text=stats)
        self._refresh_compare()

    def _toggle_stats_target(self) -> None:
        if self.companion_pil is None:
            return
        self._stats_target = "companion" if self._stats_target == "main" else "main"
        is_comp = self._stats_target == "companion"
        self.btn_stats_target.config(
            text="Stats principal" if is_comp else "Stats compagnon",
            bg="#4a2a4a" if is_comp else "#2a3a4a",
        )
        self._refresh_panel()

    def _build_segment_sequences(self) -> list[str]:
        """Séquences par 25cm : GT (CSV brut) à gauche, moi à droite."""
        use_comp  = self._stats_target == "companion" and self.companion_pil is not None
        img_h     = self.companion_pil.height if use_comp else self.img_h
        pea_pts   = self.companion_pea_points   if use_comp else self.pea_points
        wheat_pts = self.companion_wheat_points if use_comp else self.wheat_points

        if self.pixel_mm <= 0 or img_h <= 0:
            return []
        seg_px = 250.0 / self.pixel_mm
        n_seg  = max(1, math.ceil(img_h / seg_px))

        # mes séquences par segment
        buckets: list[list[tuple[float, str]]] = [[] for _ in range(n_seg)]
        for _x, y in pea_pts:
            buckets[min(int(y / seg_px), n_seg - 1)].append((y, "p"))
        for _x, y in wheat_pts:
            buckets[min(int(y / seg_px), n_seg - 1)].append((y, "w"))

        my_seqs: list[str] = []
        for pts in buckets:
            pts.sort(key=lambda t: t[0])
            seq: list[tuple[int, str]] = []
            for _, sp in pts:
                if seq and seq[-1][1] == sp:
                    seq[-1] = (seq[-1][0] + 1, sp)
                else:
                    seq.append((1, sp))
            my_seqs.append("".join(f"{n}{sp}" for n, sp in seq) if seq else "–")

        # séquences GT depuis le CSV brut (4 tranches de 25cm)
        target_path = IMG_DIR / self.companion_name if use_comp else self.current_path
        stem    = self._csv_key_from_path(target_path)
        gt_list = self.raw_seqs.get(stem, [])

        def _seq_wp(seq: str) -> str:
            """Wheat/pea counts from a sequence string, e.g. '2w3p1w' → '3w/3p'."""
            import re as _re
            w = sum(int(m.group(1)) for m in _re.finditer(r'(\d+)w', seq))
            p = sum(int(m.group(1)) for m in _re.finditer(r'(\d+)p', seq))
            return f"{w}w/{p}p"

        lines = []
        for i, my in enumerate(my_seqs):
            gt = gt_list[i] if i < len(gt_list) else "–"
            if not gt:
                gt = "–"
            gt_tag = _seq_wp(gt) if gt != "–" else "–"
            my_w   = sum(1 for _, sp in buckets[i] if sp == "w")
            my_p   = sum(1 for _, sp in buckets[i] if sp == "p")
            my_tag = f"{my_w}w/{my_p}p"
            lines.append(f"{i * 25:3d}cm GT:{gt} [{gt_tag}]")
            lines.append(f"     Moi:{my} [{my_tag}]")
        return lines

    def _refresh_compare(self) -> None:
        """Update the comparison label from CSV snapshot + current annotations."""
        if not hasattr(self, "current_path"):
            self.lbl_compare.config(text="")
            return

        use_comp    = self._stats_target == "companion" and self.companion_pil is not None
        target_path = (IMG_DIR / self.companion_name) if use_comp else self.current_path

        lines: list[str] = []

        # bloc comparaison GT (seulement si CSV disponible)
        if self.compare_data:
            # extract angle from stem before _csv_key_from_path strips it
            _stem = target_path.stem.split("_tile_")[0]
            _angle = ""
            for _a in ("_45", "_90"):
                if _stem.endswith(_a):
                    _angle = _a[1:]
                    break
            base_key = self._csv_key_from_path(target_path)
            key = f"{base_key}_{_angle}" if _angle else base_key
            row = self.compare_data.get(key)
            if row is None:
                lines.append("(pas dans le CSV de comparaison)")
            else:
                def _err_line(label: str, err_str: str, rel_str: str) -> str:
                    if err_str == "":
                        return f"{label} : (non annoté)"
                    err  = float(err_str)
                    sign = "+" if err > 0 else ""
                    direction = "surcompté" if err > 0 else "sous-compté" if err < 0 else "exact"
                    rel_part  = f" ({sign}{rel_str}%)" if rel_str != "" else ""
                    return f"{label} : {sign}{int(err)}{rel_part}  {direction}"

                lines.append("─ Comparaison GT ─")
                ptype = row.get("type", "")
                if ptype in ("wheat", "mixed"):
                    lines.append(_err_line("Blé ",
                                           row.get("wheat_err", ""),
                                           row.get("wheat_rel_err_pct", "")))
                if ptype in ("pea", "mixed"):
                    lines.append(_err_line("Pois",
                                           row.get("pea_err", ""),
                                           row.get("pea_rel_err_pct", "")))
                true_seq = row.get("true_sequence", "")
                my_seq   = row.get("my_sequence",   "")
                if true_seq or my_seq:
                    lines.append(f"Vrai : {true_seq or '–'}")
                    lines.append(f"Moi  : {my_seq  or '–'}")

        # bloc séquences par 25cm (annotations courantes, mis à jour en direct)
        seg_lines = self._build_segment_sequences()
        if seg_lines:
            if lines:
                lines.append("")
            lines.append("─ Par 25cm ─")
            lines.extend(seg_lines)

        self.lbl_compare.config(text="\n".join(lines), fg="#aaaaaa")

    # ── Image loading & rendering ──────────────────────────────────────────────

    def _load_image(self) -> None:
        path = self.images[self.idx]
        self.current_path = path
        self._dirty = False
        name  = path.name
        stem  = path.stem

        # reset annotation buffers
        self.pea_points   = []
        self.wheat_points = []
        self.pea_boxes    = []
        self.wheat_boxes  = []
        self._cancel_rect()
        self._cancel_companion_rect()
        self.zoom  = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0

        # split assignment for this plot — split.json first, then annotation JSON
        self.plot_id       = path.stem.split("_tile_")[0]
        file_split         = self._get_split_file(self.plot_id)
        ann_split          = self._get_plot_split(self.plot_id)
        inferred_split     = file_split if file_split is not None else ann_split
        self.split         = inferred_split
        self.split_locked  = inferred_split is not None

        # load existing annotations
        ext       = path.suffix
        key_pea   = f"{stem}_pea{ext}"
        key_wheat = f"{stem}_wheat{ext}"

        if key_pea in self.annotations or key_wheat in self.annotations:
            # mixed image already annotated
            self.category = "mixed"
            self.species  = "pea"
            if key_pea in self.annotations:
                ann = self.annotations[key_pea]
                self.pea_points = [list(p) for p in ann.get("points", [])]
                self.pea_boxes  = [fsc_to_xyxy(b)
                                   for b in ann.get("box_examples_coordinates", [])]
            if key_wheat in self.annotations:
                ann = self.annotations[key_wheat]
                self.wheat_points = [list(p) for p in ann.get("points", [])]
                self.wheat_boxes  = [fsc_to_xyxy(b)
                                     for b in ann.get("box_examples_coordinates", [])]
        elif name in self.annotations:
            ann = self.annotations[name]
            cat = ann.get("category", "pea")
            self.category = cat
            self.species  = cat
            if cat == "pea":
                self.pea_points = [list(p) for p in ann.get("points", [])]
                self.pea_boxes  = [fsc_to_xyxy(b)
                                   for b in ann.get("box_examples_coordinates", [])]
            else:
                self.wheat_points = [list(p) for p in ann.get("points", [])]
                self.wheat_boxes  = [fsc_to_xyxy(b)
                                     for b in ann.get("box_examples_coordinates", [])]
        else:
            # new image — default category
            self.category = "pea"
            self.species  = "pea"

        # inherit & lock category at plot level — CSV takes priority, then JSON
        csv_cat      = self._get_csv_category(self.plot_id)
        json_cat     = self._get_plot_category(self.plot_id)
        inferred_cat = csv_cat if csv_cat is not None else json_cat
        if inferred_cat is not None:
            self.category = inferred_cat
            if inferred_cat in ("pea", "wheat"):
                self.species = inferred_cat
        self.category_locked = inferred_cat is not None

        self.pil_img = Image.open(path).convert("RGB")
        self.img_w, self.img_h = self.pil_img.size

        # résolution réelle en mm/px depuis les coins L93 du transect
        _corners = self.linm_corners.get(self._csv_key_from_path(path))
        if _corners and self.img_h > 1:
            self.pixel_mm = math.dist(_corners["TL"], _corners["BL"]) / self.img_h * 1000.0
        else:
            self.pixel_mm = 0.7  # fallback si transect absent de linm_ref.json

        screen_h = max(self.root.winfo_screenheight() - 80, 600)
        self.scale  = screen_h / self.img_h
        # viewport dimensions: each image half = (screen_w - panel) / 2
        self._vp_h  = screen_h
        self._vp_w  = max((self.root.winfo_screenwidth() - PANEL_W) // 2, 100)

        # companion image (opposite angle)
        comp_path = self._find_companion_path(path)
        if comp_path is not None:
            self.companion_pil  = Image.open(comp_path).convert("RGB")
            self.companion_name = comp_path.name
            (self.companion_pea_points, self.companion_wheat_points,
             self.companion_pea_boxes,  self.companion_wheat_boxes) = \
                self._load_companion_annotations(comp_path)
        else:
            self.companion_pil          = None
            self.companion_name         = ""
            self.companion_pea_points   = []
            self.companion_wheat_points = []
            self.companion_pea_boxes    = []
            self.companion_wheat_boxes  = []

        self.lbl_file.config(text=path.name)
        self.lbl_idx.config( text=f"Image {self.idx + 1} / {len(self.images)}")

        # reset stats toggle; gray out button when no companion
        self._stats_target = "main"
        if self.companion_pil is not None:
            self.btn_stats_target.config(text="Stats compagnon", bg="#2a3a4a",
                                         fg="white", cursor="hand2")
        else:
            self.btn_stats_target.config(text="Stats compagnon", bg="#1c1c1c",
                                         fg="#333333", cursor="")

        self._redraw()
        self._refresh_panel()
        self._refresh_compare()

    def _redraw(self) -> None:
        s  = self.scale * self.zoom
        cw = int(self.img_w * s)
        ch = int(self.img_h * s)
        px, py = int(self.pan_x), int(self.pan_y)

        scaled      = self.pil_img.resize((cw, ch), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(scaled)

        self.canvas.delete("all")
        self.canvas.create_image(px, py, anchor=tk.NW, image=self._photo)

        r = POINT_R
        for x, y in self.pea_points:
            cx, cy = x * s + px, y * s + py
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                    fill=COLOR_PEA, outline="white", width=1)
        for x, y in self.wheat_points:
            cx, cy = x * s + px, y * s + py
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                    fill=COLOR_WHEAT, outline="white", width=1)
        for b in self.pea_boxes:
            self.canvas.create_rectangle(
                b[0]*s + px, b[1]*s + py, b[2]*s + px, b[3]*s + py,
                outline=COLOR_PEA, width=BOX_W)
        for b in self.wheat_boxes:
            self.canvas.create_rectangle(
                b[0]*s + px, b[1]*s + py, b[2]*s + px, b[3]*s + py,
                outline=COLOR_WHEAT, width=BOX_W)

        # lignes pointillées rouges toutes les 25cm (haut → bas)
        if self.pixel_mm > 0:
            seg_px = 250.0 / self.pixel_mm
            x0, x1 = px, px + self.img_w * s
            y_img = seg_px
            while y_img < self.img_h:
                cy = y_img * s + py
                self.canvas.create_line(x0, cy, x1, cy,
                                        fill="#cc3333", width=1, dash=(2, 5))
                y_img += seg_px

        self.rect_prev_id = None
        self._redraw_companion()

    # ── Companion helpers ─────────────────────────────────────────────────────

    def _find_companion_path(self, path: Path) -> Optional[Path]:
        """Return the path to the opposite-angle image, or None if absent."""
        stem = path.stem
        if stem.endswith("_45"):
            comp_stem = stem[:-3] + "90"
        elif stem.endswith("_90"):
            comp_stem = stem[:-2] + "45"
        else:
            return None
        comp_path = path.parent / (comp_stem + path.suffix)
        return comp_path if comp_path.exists() else None

    def _load_companion_annotations(self, comp_path: Path) -> tuple:
        """Return (pea_pts, wheat_pts, pea_boxes, wheat_boxes) for the companion image."""
        name = comp_path.name
        stem = comp_path.stem
        ext  = comp_path.suffix
        pea_pts, wheat_pts, pea_boxes, wheat_boxes = [], [], [], []

        key_pea   = f"{stem}_pea{ext}"
        key_wheat = f"{stem}_wheat{ext}"
        if key_pea in self.annotations or key_wheat in self.annotations:
            if key_pea in self.annotations:
                ann = self.annotations[key_pea]
                pea_pts   = [list(p) for p in ann.get("points", [])]
                pea_boxes = [fsc_to_xyxy(b) for b in ann.get("box_examples_coordinates", [])]
            if key_wheat in self.annotations:
                ann = self.annotations[key_wheat]
                wheat_pts   = [list(p) for p in ann.get("points", [])]
                wheat_boxes = [fsc_to_xyxy(b) for b in ann.get("box_examples_coordinates", [])]
        elif name in self.annotations:
            ann = self.annotations[name]
            cat = ann.get("category", "pea")
            if cat == "pea":
                pea_pts   = [list(p) for p in ann.get("points", [])]
                pea_boxes = [fsc_to_xyxy(b) for b in ann.get("box_examples_coordinates", [])]
            else:
                wheat_pts   = [list(p) for p in ann.get("points", [])]
                wheat_boxes = [fsc_to_xyxy(b) for b in ann.get("box_examples_coordinates", [])]
        return pea_pts, wheat_pts, pea_boxes, wheat_boxes

    def _redraw_companion(self) -> None:
        """Render companion image (read-only) with its annotations at the same zoom/pan."""
        self.companion_canvas.delete("all")
        if self.companion_pil is None:
            self.companion_canvas.create_text(
                10, 10, anchor="nw",
                text="(pas d'image compagnon)",
                fill="#444444", font=("Helvetica", 10),
            )
            return

        s  = self.scale * self.zoom
        cw = int(self.companion_pil.width  * s)
        ch = int(self.companion_pil.height * s)
        px, py = int(self.pan_x), int(self.pan_y)

        scaled = self.companion_pil.resize((cw, ch), Image.LANCZOS)
        self._companion_photo = ImageTk.PhotoImage(scaled)
        self.companion_canvas.create_image(px, py, anchor=tk.NW, image=self._companion_photo)

        r = POINT_R
        for x, y in self.companion_pea_points:
            cx, cy = x * s + px, y * s + py
            self.companion_canvas.create_oval(cx-r, cy-r, cx+r, cy+r,
                                              fill=COLOR_PEA, outline="white", width=1)
        for x, y in self.companion_wheat_points:
            cx, cy = x * s + px, y * s + py
            self.companion_canvas.create_oval(cx-r, cy-r, cx+r, cy+r,
                                              fill=COLOR_WHEAT, outline="white", width=1)
        for b in self.companion_pea_boxes:
            self.companion_canvas.create_rectangle(
                b[0]*s+px, b[1]*s+py, b[2]*s+px, b[3]*s+py,
                outline=COLOR_PEA, width=BOX_W)
        for b in self.companion_wheat_boxes:
            self.companion_canvas.create_rectangle(
                b[0]*s+px, b[1]*s+py, b[2]*s+px, b[3]*s+py,
                outline=COLOR_WHEAT, width=BOX_W)

        if self.pixel_mm > 0:
            seg_px = 250.0 / self.pixel_mm
            x0 = px
            x1 = px + self.companion_pil.width * s
            y_img = seg_px
            while y_img < self.companion_pil.height:
                cy = y_img * s + py
                self.companion_canvas.create_line(x0, cy, x1, cy,
                                                  fill="#cc3333", width=1, dash=(2, 5))
                y_img += seg_px

        # small label overlay
        self.companion_canvas.create_text(
            6, 6, anchor="nw",
            text=self.companion_name,
            fill="#888888", font=("Helvetica", 8),
        )

    # ── Mouse events ──────────────────────────────────────────────────────────

    def _on_click(self, ev: tk.Event) -> None:
        s  = self.scale * self.zoom
        ix = max(0.0, min(float(self.img_w - 1), (ev.x - self.pan_x) / s))
        iy = max(0.0, min(float(self.img_h - 1), (ev.y - self.pan_y) / s))
        sp = self._active_species()

        if self.mode == "write":
            if self.tool == "point":
                self._add_point(ix, iy, sp)
            else:
                self._handle_rect_click(ev.x, ev.y, ix, iy, sp)
        else:  # erase
            if self.tool == "point":
                self._erase_nearest_point(ix, iy, sp)
            else:
                self._erase_nearest_box(ix, iy, sp)

    def _on_motion(self, ev: tk.Event) -> None:
        if self.mode != "write" or self.tool != "rect":
            return
        if self.rect_anchor is None:
            return
        if self.rect_prev_id is not None:
            self.canvas.delete(self.rect_prev_id)
        color = COLOR_PEA if self._active_species() == "pea" else COLOR_WHEAT
        ax, ay = self.rect_anchor
        self.rect_prev_id = self.canvas.create_rectangle(
            ax, ay, ev.x, ev.y, outline=color, width=2, dash=(4, 2)
        )

    # ── Annotation actions ────────────────────────────────────────────────────

    def _add_point(self, ix: float, iy: float, sp: str) -> None:
        pts = self.pea_points if sp == "pea" else self.wheat_points
        pts.append([ix, iy])
        self._dirty = True
        s  = self.scale * self.zoom
        px, py = int(self.pan_x), int(self.pan_y)
        r  = POINT_R
        color = COLOR_PEA if sp == "pea" else COLOR_WHEAT
        self.canvas.create_oval(
            ix*s + px - r, iy*s + py - r,
            ix*s + px + r, iy*s + py + r,
            fill=color, outline="white", width=1)
        self._refresh_panel()

    def _handle_rect_click(self, cx: int, cy: int,
                           ix: float, iy: float, sp: str) -> None:
        if self.rect_anchor is None:
            self.rect_anchor = (cx, cy)
            return

        ax, ay  = self.rect_anchor
        x1c, y1c = min(ax, cx), min(ay, cy)
        x2c, y2c = max(ax, cx), max(ay, cy)

        if (x2c - x1c) < 8 or (y2c - y1c) < 8:
            self._cancel_rect()
            return

        s  = self.scale * self.zoom
        px, py = self.pan_x, self.pan_y
        box = [(x1c - px)/s, (y1c - py)/s, (x2c - px)/s, (y2c - py)/s]
        (self.pea_boxes if sp == "pea" else self.wheat_boxes).append(box)
        self._dirty = True

        if self.rect_prev_id is not None:
            self.canvas.delete(self.rect_prev_id)
            self.rect_prev_id = None
        color = COLOR_PEA if sp == "pea" else COLOR_WHEAT
        self.canvas.create_rectangle(x1c, y1c, x2c, y2c, outline=color, width=BOX_W)

        self.rect_anchor = None
        self._refresh_panel()

    def _erase_nearest_point(self, ix: float, iy: float, sp: str) -> None:
        pts = self.pea_points if sp == "pea" else self.wheat_points
        if not pts:
            return
        i = min(range(len(pts)), key=lambda k: dist2(ix, iy, pts[k][0], pts[k][1]))
        pts.pop(i)
        self._dirty = True
        self._redraw()
        self._refresh_panel()

    def _erase_nearest_box(self, ix: float, iy: float, sp: str) -> None:
        boxes = self.pea_boxes if sp == "pea" else self.wheat_boxes
        if not boxes:
            return
        i = min(range(len(boxes)), key=lambda k: dist2(ix, iy, *box_center(boxes[k])))
        boxes.pop(i)
        self._dirty = True
        self._redraw()
        self._refresh_panel()

    # ── Companion annotation actions ──────────────────────────────────────────

    def _on_companion_click(self, ev: tk.Event) -> None:
        if self.companion_pil is None:
            return
        s  = self.scale * self.zoom
        cw = self.companion_pil.width
        ch = self.companion_pil.height
        ix = max(0.0, min(float(cw - 1), (ev.x - self.pan_x) / s))
        iy = max(0.0, min(float(ch - 1), (ev.y - self.pan_y) / s))
        sp = self._active_species()
        if self.mode == "write":
            if self.tool == "point":
                self._add_companion_point(ix, iy, sp)
            else:
                self._handle_companion_rect_click(ev.x, ev.y, ix, iy, sp)
        else:
            if self.tool == "point":
                self._erase_companion_nearest_point(ix, iy, sp)
            else:
                self._erase_companion_nearest_box(ix, iy, sp)

    def _on_companion_motion(self, ev: tk.Event) -> None:
        if self.mode != "write" or self.tool != "rect":
            return
        if self.companion_rect_anchor is None:
            return
        if self.companion_rect_prev_id is not None:
            self.companion_canvas.delete(self.companion_rect_prev_id)
        color = COLOR_PEA if self._active_species() == "pea" else COLOR_WHEAT
        ax, ay = self.companion_rect_anchor
        self.companion_rect_prev_id = self.companion_canvas.create_rectangle(
            ax, ay, ev.x, ev.y, outline=color, width=2, dash=(4, 2)
        )

    def _add_companion_point(self, ix: float, iy: float, sp: str) -> None:
        pts = self.companion_pea_points if sp == "pea" else self.companion_wheat_points
        pts.append([ix, iy])
        self._dirty = True
        s  = self.scale * self.zoom
        px, py = int(self.pan_x), int(self.pan_y)
        r  = POINT_R
        color = COLOR_PEA if sp == "pea" else COLOR_WHEAT
        self.companion_canvas.create_oval(
            ix*s + px - r, iy*s + py - r,
            ix*s + px + r, iy*s + py + r,
            fill=color, outline="white", width=1)
        self._refresh_panel()

    def _handle_companion_rect_click(self, cx: int, cy: int,
                                     ix: float, iy: float, sp: str) -> None:
        if self.companion_rect_anchor is None:
            self.companion_rect_anchor = (cx, cy)
            return
        ax, ay  = self.companion_rect_anchor
        x1c, y1c = min(ax, cx), min(ay, cy)
        x2c, y2c = max(ax, cx), max(ay, cy)
        if (x2c - x1c) < 8 or (y2c - y1c) < 8:
            self._cancel_companion_rect()
            return
        s  = self.scale * self.zoom
        px, py = self.pan_x, self.pan_y
        box = [(x1c - px)/s, (y1c - py)/s, (x2c - px)/s, (y2c - py)/s]
        (self.companion_pea_boxes if sp == "pea" else self.companion_wheat_boxes).append(box)
        self._dirty = True
        if self.companion_rect_prev_id is not None:
            self.companion_canvas.delete(self.companion_rect_prev_id)
            self.companion_rect_prev_id = None
        color = COLOR_PEA if sp == "pea" else COLOR_WHEAT
        self.companion_canvas.create_rectangle(x1c, y1c, x2c, y2c, outline=color, width=BOX_W)
        self.companion_rect_anchor = None
        self._refresh_panel()

    def _erase_companion_nearest_point(self, ix: float, iy: float, sp: str) -> None:
        pts = self.companion_pea_points if sp == "pea" else self.companion_wheat_points
        if not pts:
            return
        i = min(range(len(pts)), key=lambda k: dist2(ix, iy, pts[k][0], pts[k][1]))
        pts.pop(i)
        self._dirty = True
        self._redraw()
        self._refresh_panel()

    def _erase_companion_nearest_box(self, ix: float, iy: float, sp: str) -> None:
        boxes = self.companion_pea_boxes if sp == "pea" else self.companion_wheat_boxes
        if not boxes:
            return
        i = min(range(len(boxes)), key=lambda k: dist2(ix, iy, *box_center(boxes[k])))
        boxes.pop(i)
        self._dirty = True
        self._redraw()
        self._refresh_panel()

    # ── Zoom & pan ────────────────────────────────────────────────────────────

    def _on_wheel(self, ev: tk.Event) -> None:
        """Zoom in/out on scroll wheel (real mouse or two-finger trackpad)."""
        if ev.delta == 0:
            return
        factor = 1.15 if ev.delta > 0 else (1.0 / 1.15)
        self._do_zoom(factor, ev.x, ev.y)

    def _on_magnify(self, ev: tk.Event) -> None:
        """Zoom in/out on macOS pinch gesture."""
        factor = 1.0 + ev.delta
        if factor > 0.05:
            self._do_zoom(factor, ev.x, ev.y)

    def _do_zoom(self, factor: float, cx: float, cy: float) -> None:
        """Apply zoom factor centered on canvas point (cx, cy)."""
        new_zoom = max(1.0, min(_MAX_ZOOM, self.zoom * factor))
        # keep the image point under the cursor fixed on screen
        self.pan_x = cx - (cx - self.pan_x) * new_zoom / self.zoom
        self.pan_y = cy - (cy - self.pan_y) * new_zoom / self.zoom
        self.zoom  = new_zoom
        self._clamp_pan()
        self._redraw()

    def _clamp_pan(self) -> None:
        """Clamp pan so the image stays within its viewport half."""
        vp_w = getattr(self, '_vp_w', int(self.img_w * self.scale))
        vp_h = getattr(self, '_vp_h', int(self.img_h * self.scale))
        zoomed_w = self.img_w * self.scale * self.zoom
        zoomed_h = self.img_h * self.scale * self.zoom
        # horizontal: image smaller than viewport → pin left; larger → allow scroll
        if zoomed_w <= vp_w:
            self.pan_x = 0.0
        else:
            self.pan_x = max(vp_w - zoomed_w, min(0.0, self.pan_x))
        # vertical
        if zoomed_h <= vp_h:
            self.pan_y = 0.0
        else:
            self.pan_y = max(vp_h - zoomed_h, min(0.0, self.pan_y))

    def _on_pan_start(self, ev: tk.Event) -> None:
        self._cancel_rect()
        self._cancel_companion_rect()
        self._pan_anchor = (ev.x, ev.y)
        self.canvas.config(cursor="fleur")
        self.companion_canvas.config(cursor="fleur")

    def _on_pan_drag(self, ev: tk.Event) -> None:
        if self._pan_anchor is None:
            return
        self.pan_x += ev.x - self._pan_anchor[0]
        self.pan_y += ev.y - self._pan_anchor[1]
        self._pan_anchor = (ev.x, ev.y)
        self._clamp_pan()
        self._redraw()

    def _on_pan_end(self, _ev: tk.Event) -> None:
        self._pan_anchor = None
        self.canvas.config(cursor="crosshair")
        self.companion_canvas.config(cursor="crosshair")

    # ── Serialization ─────────────────────────────────────────────────────────

    def _check_boxes(self) -> bool:
        """Warn if < 3 exemplar boxes or split not set; return True to proceed, False to cancel."""
        if self.split is None:
            messagebox.showwarning(
                "Split non défini",
                f"Assigne Train / Val / Test à la parcelle {self.plot_id} avant de sauvegarder.",
            )
            return False
        problems = []
        if self.category in ("pea", "mixed") and len(self.pea_boxes) < 3:
            problems.append(f"Pois : {len(self.pea_boxes)}/3 boîtes exemplaires")
        if self.category in ("wheat", "mixed") and len(self.wheat_boxes) < 3:
            problems.append(f"Blé : {len(self.wheat_boxes)}/3 boîtes exemplaires")
        if not problems:
            return True
        msg = (f"Boîtes manquantes pour {self.current_path.name} :\n"
               + "\n".join(problems)
               + "\n\nSauvegarder quand même ?")
        return messagebox.askyesno("Boîtes manquantes", msg, icon="warning")

    def _make_entry(self, points: list, boxes: list,
                    category: str, image_file: Optional[str] = None) -> dict:
        entry: dict = {
            "points":                   [list(p) for p in points],
            "box_examples_coordinates": [xyxy_to_fsc(*b) for b in boxes[:3]],
            "category":                 category,
            "split":                    self.split,
        }
        if image_file:
            entry["image_file"] = image_file
        return entry

    def _commit(self) -> None:
        """Write current image's annotations into self.annotations (in memory)."""
        name  = self.current_path.name
        stem  = self.current_path.stem
        ext   = self.current_path.suffix
        kp    = f"{stem}_pea{ext}"
        kw    = f"{stem}_wheat{ext}"

        if self.category == "pea":
            # remove any stale mixed entries
            self.annotations.pop(kp, None)
            self.annotations.pop(kw, None)
            self.annotations[name] = self._make_entry(
                self.pea_points, self.pea_boxes, "pea"
            )
        elif self.category == "wheat":
            self.annotations.pop(kp, None)
            self.annotations.pop(kw, None)
            self.annotations[name] = self._make_entry(
                self.wheat_points, self.wheat_boxes, "wheat"
            )
        else:  # mixed
            # remove any pure entry that may exist
            self.annotations.pop(name, None)
            self.annotations[kp] = self._make_entry(
                self.pea_points, self.pea_boxes, "mixed_pea", image_file=name
            )
            self.annotations[kw] = self._make_entry(
                self.wheat_points, self.wheat_boxes, "mixed_wheat", image_file=name
            )

    def _commit_companion(self) -> None:
        """Write companion annotations into self.annotations under the companion's key."""
        if not self.companion_name or self.companion_pil is None:
            return
        comp_path = IMG_DIR / self.companion_name
        name = comp_path.name
        stem = comp_path.stem
        ext  = comp_path.suffix
        kp   = f"{stem}_pea{ext}"
        kw   = f"{stem}_wheat{ext}"
        if self.category == "pea":
            self.annotations.pop(kp, None)
            self.annotations.pop(kw, None)
            self.annotations[name] = self._make_entry(
                self.companion_pea_points, self.companion_pea_boxes, "pea"
            )
        elif self.category == "wheat":
            self.annotations.pop(kp, None)
            self.annotations.pop(kw, None)
            self.annotations[name] = self._make_entry(
                self.companion_wheat_points, self.companion_wheat_boxes, "wheat"
            )
        else:
            self.annotations.pop(name, None)
            self.annotations[kp] = self._make_entry(
                self.companion_pea_points, self.companion_pea_boxes, "mixed_pea", image_file=name
            )
            self.annotations[kw] = self._make_entry(
                self.companion_wheat_points, self.companion_wheat_boxes, "mixed_wheat", image_file=name
            )

    def _commit_all(self) -> None:
        """Commit both main and companion annotations to memory."""
        self._commit()
        if self.companion_name:
            self._commit_companion()

    def _flush(self) -> None:
        ANN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ANN_FILE, "w") as f:
            json.dump(self.annotations, f, indent=2, ensure_ascii=False)
        self._dirty = False

    # ── Navigation ────────────────────────────────────────────────────────────

    def _save(self) -> None:
        if not self._check_boxes():
            return
        self._commit_all()
        self._flush()
        messagebox.showinfo("Sauvegardé",
                            f"Annotations sauvegardées dans\n{ANN_FILE.name}")

    def _ask_nav_save(self) -> Optional[bool]:
        """Ask whether to save before leaving. Returns True=save, False=skip, None=cancel."""
        return messagebox.askyesnocancel(
            "Modifications non sauvegardées",
            f"Sauvegarder {self.current_path.name} avant de continuer ?",
        )

    def _go_prev(self) -> None:
        if self.idx == 0:
            return
        if self._dirty:
            resp = self._ask_nav_save()
            if resp is None:
                return
            if resp:
                if not self._check_boxes():
                    return
                self._commit_all()
                self._flush()
        self.idx -= 1
        self._load_image()

    def _go_next(self) -> None:
        if self._dirty:
            resp = self._ask_nav_save()
            if resp is None:
                return
            if resp:
                if not self._check_boxes():
                    return
                self._commit_all()
                self._flush()
        if self.idx >= len(self.images) - 1:
            messagebox.showinfo("Terminé",
                                "Toutes les images ont été parcourues.\n"
                                f"Annotations dans {ANN_FILE.name}")
            return
        self.idx += 1
        self._load_image()

    def _go_first_unannotated(self) -> None:
        """Jump to the first image that has no annotation entry yet."""
        if self._dirty:
            resp = self._ask_nav_save()
            if resp is None:
                return
            if resp:
                if not self._check_boxes():
                    return
                self._commit_all()
                self._flush()
        for i, path in enumerate(self.images):
            stem, ext = path.stem, path.suffix
            keys = (path.name, f"{stem}_pea{ext}", f"{stem}_wheat{ext}")
            if not any(k in self.annotations for k in keys):
                self.idx = i
                self._load_image()
                return
        messagebox.showinfo("Tout annoté", "Toutes les images ont déjà une annotation.")

    def _reset(self) -> None:
        self._cancel_rect()
        if self.category == "mixed":
            choice = simpledialog.askstring(
                "Réinitialiser",
                "Que réinitialiser ?\n  pea  /  wheat  /  all",
                initialvalue="all",
            )
            if choice is None:
                return
            c = choice.strip().lower()
            if c in ("pea", "pois", "p"):
                self.pea_points.clear()
                self.pea_boxes.clear()
            elif c in ("wheat", "blé", "ble", "w"):
                self.wheat_points.clear()
                self.wheat_boxes.clear()
            else:
                self.pea_points.clear()
                self.pea_boxes.clear()
                self.wheat_points.clear()
                self.wheat_boxes.clear()
        else:
            if not messagebox.askyesno("Réinitialiser",
                                       "Supprimer tous les points et boîtes ?"):
                return
            self.pea_points.clear()
            self.pea_boxes.clear()
            self.wheat_points.clear()
            self.wheat_boxes.clear()

        self._dirty = True
        self._redraw()
        self._refresh_panel()


    def _reload_compare(self) -> None:
        """Re-run compare_linm.py then reload the CSV into memory."""
        result = subprocess.run(
            [sys.executable, str(COMPARE_SCRIPT)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            messagebox.showerror("Erreur compare_linm", result.stderr[-600:] or result.stdout[-600:])
            return
        self.compare_data.clear()
        if COMPARE_CSV.exists():
            with open(COMPARE_CSV, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    angle_val = row.get("angle", "")
                    suffix = f"_{angle_val}" if angle_val else ""
                    key = f"{row['parcelle']}_{row['metre_lineaire']}{suffix}"
                    self.compare_data[key] = row
        # reload companion annotations in case they changed
        if self.companion_pil is not None and self.companion_name:
            comp_path = IMG_DIR / self.companion_name
            if comp_path.exists():
                (self.companion_pea_points, self.companion_wheat_points,
                 self.companion_pea_boxes,  self.companion_wheat_boxes) = \
                    self._load_companion_annotations(comp_path)
                self._redraw_companion()

        self._refresh_compare()
        self.lbl_search_result.config(text="GT rechargé ✓", fg="#2e7a2e")

    def _do_search(self) -> None:
        """Jump to the first (or next) image whose key matches the search query."""
        query = self.search_var.get().strip().lower()
        if not query:
            return
        matches = [i for i, p in enumerate(self.images)
                   if query in self._csv_key_from_path(p).lower()]
        if not matches:
            self.lbl_search_result.config(
                text=f"Aucun résultat pour « {query} »", fg="#8a3a3a"
            )
            return
        # cycle : next match after current position, wrap around
        next_idx = next((i for i in matches if i > self.idx), matches[0])
        if self._dirty:
            resp = self._ask_nav_save()
            if resp is None:
                return
            if resp:
                if not self._check_boxes():
                    return
                self._commit_all()
                self._flush()
        self.idx = next_idx
        pos = matches.index(next_idx) + 1
        self.lbl_search_result.config(
            text=f"{len(matches)} résultat(s)  [{pos}/{len(matches)}]", fg="#556666"
        )
        self._load_image()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Annotateur wheatpea")
    parser.add_argument(
        "--angle", choices=["45", "90"],
        help="Filtre les images par angle de vol (45 ou 90). Sans cet argument, toutes les images sont affichées.",
    )
    args = parser.parse_args()

    if not IMG_DIR.exists():
        print(f"Dossier images introuvable :\n  {IMG_DIR}")
        sys.exit(1)

    exts = ("*.jpg", "*.JPG", "*.jpeg", "*.JPEG", "*.png", "*.PNG",
            "*.tif", "*.TIF", "*.tiff", "*.TIFF")
    images: list[Path] = []
    for ext in exts:
        images.extend(IMG_DIR.glob(ext))
    images = sorted(set(images))

    if args.angle:
        images = [p for p in images if p.stem.endswith(f"_{args.angle}")]
        print(f"Filtre angle={args.angle} : {len(images)} images")

    if not images:
        print(f"Aucune image dans {IMG_DIR}")
        sys.exit(1)

    print(f"{len(images)} images à annoter")
    print(f"Annotations → {ANN_FILE}")
    if ANN_FILE.exists():
        with open(ANN_FILE) as f:
            existing = json.load(f)
        print(f"  ({len(existing)} entrées déjà présentes)")

    root = tk.Tk()
    Annotator(root, images)
    root.mainloop()


if __name__ == "__main__":
    main()
