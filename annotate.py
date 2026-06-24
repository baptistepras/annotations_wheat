#!/usr/bin/env python3
"""Annotation tool for wheat tip counting in pointes/.

Usage:
    python pointes/annotate.py

Controls:
    Mode Écrire  : clic → place un point
    Mode Effacer : clic → supprime le point le plus proche

Scroll / pinch → zoom
Right-click drag (ou bouton du milieu) → pan

Sauvegarde dans pointes/annotations.json.

Sources d'images :
    - 2025-12-15_phénotypage-selfie-stick/x76y13/     (toutes sauf les 2 premières)
    - 2025-12-15_phénotypage-selfie-stick/x81y12/     (toutes sauf les 2 premières)
    - 2025-12-15_phénotypage-selfie-stick/3photos_per_plot/
        xXXyYY_IMG_20251215_AAAAAA.jpg : 4 images par parcelle, on exclut la
        première par ordre lexicographique sur AAAAAA (la plus ancienne).
"""

import json
import tkinter as tk
from tkinter import messagebox
from pathlib import Path
from typing import Optional
from collections import defaultdict

from PIL import Image, ImageTk

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE    = Path(__file__).resolve().parent
ANN_FILE = _HERE / "annotations.json"

BASE_DIR = _HERE / "2025-12-15_phénotypage-selfie-stick"
DIR_X76  = BASE_DIR / "x76y13"
DIR_X81  = BASE_DIR / "x81y12"
DIR_3PP  = BASE_DIR / "3photos_per_plot"

# ── Appearance ────────────────────────────────────────────────────────────────
COLOR_PT  = "#FF4444"
POINT_R   = 5
PANEL_W   = 280
_MAX_ZOOM = 10.0

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def dist2(ax: float, ay: float, bx: float, by: float) -> float:
    """Squared Euclidean distance."""
    return (ax - bx) ** 2 + (ay - by) ** 2


def collect_images() -> list[Path]:
    """Collect and filter images from all three source directories."""
    images: list[Path] = []

    # x76y13 and x81y12: sort lexicographically, skip the first 2
    for d in (DIR_X76, DIR_X81):
        if d.exists():
            imgs = sorted(p for p in d.iterdir() if p.suffix.lower() in _IMG_EXTS)
            images.extend(imgs[2:])

    # 3photos_per_plot: group by plot prefix (xXXyYY), skip first per group
    if DIR_3PP.exists():
        by_plot: defaultdict[str, list[Path]] = defaultdict(list)
        for p in DIR_3PP.iterdir():
            if p.suffix.lower() not in _IMG_EXTS:
                continue
            # stem looks like xXXyYY_IMG_20251215_AAAAAA
            if "_IMG_" in p.stem:
                plot_key = p.stem.split("_IMG_")[0]
                by_plot[plot_key].append(p)
        for plot_key in sorted(by_plot):
            imgs = sorted(
                by_plot[plot_key],
                key=lambda p: p.stem.split("_IMG_", 1)[1],
            )
            images.extend(imgs[1:])  # skip the one with smallest timestamp

    return images


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

        # per-image state
        self.points: list[list[float]] = []
        self.mode:   str               = "write"   # write | erase
        self._dirty: bool              = False

        # image / rendering state
        self.scale:    float                    = 1.0
        self.img_w:    int                      = 1
        self.img_h:    int                      = 1
        self.pil_img:  Optional[Image.Image]    = None
        self._photo                             = None
        self.current_path: Path                 = images[0]

        # zoom & pan
        self.zoom:        float                     = 1.0
        self.pan_x:       float                     = 0.0
        self.pan_y:       float                     = 0.0
        self._pan_anchor: Optional[tuple[int, int]] = None

        self._build_ui()
        self._load_image()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.root.title("Annotateur pointes blé")
        self.root.configure(bg="#1a1a1a")

        img_frame = tk.Frame(self.root, bg="#0d0d0d")
        img_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(img_frame, bg="#0d0d0d", cursor="crosshair",
                                highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Button-1>",   self._on_click)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        try:
            self.canvas.bind("<Magnify>", self._on_magnify)
        except Exception:
            pass
        for _b in (2, 3):
            self.canvas.bind(f"<ButtonPress-{_b}>",  self._on_pan_start)
            self.canvas.bind(f"<B{_b}-Motion>",       self._on_pan_drag)
            self.canvas.bind(f"<ButtonRelease-{_b}>", self._on_pan_end)

        panel = tk.Frame(self.root, bg="#1a1a1a", width=PANEL_W)
        panel.pack(side=tk.RIGHT, fill=tk.Y, padx=6, pady=6)
        panel.pack_propagate(False)

        def lbl(text: str = "", size: int = 10,
                fg: str = "#cccccc", **kw) -> tk.Label:
            return tk.Label(panel, text=text, bg="#1a1a1a", fg=fg,
                            font=("Helvetica", size), **kw)

        def sep() -> None:
            tk.Frame(panel, bg="#3a3a3a", height=1).pack(fill=tk.X, pady=5)

        def row_frame() -> tk.Frame:
            f = tk.Frame(panel, bg="#1a1a1a")
            f.pack(fill=tk.X, pady=(2, 4))
            return f

        def navlbl(text: str, bg: str, cmd) -> tk.Label:
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

        # mode buttons
        lbl("Mode", size=9, fg="#777777").pack(anchor="w")
        r1 = row_frame()
        self.btn_write = self._tbtn(r1, "Écrire",  lambda: self._set_mode("write"))
        self.btn_erase = self._tbtn(r1, "Effacer", lambda: self._set_mode("erase"))
        self.btn_write.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        self.btn_erase.pack(side=tk.LEFT, fill=tk.X, expand=True)

        sep()

        self.lbl_stats = lbl(size=9, fg="#aaaaaa", justify=tk.LEFT, anchor="w")
        self.lbl_stats.pack(fill=tk.X)

        sep()

        # navigation buttons
        self.btn_prev  = navlbl("◀ Précédent",        "#2a2a6a", self._go_prev)
        self.btn_save  = navlbl("💾 Sauvegarder",     "#1a5a2a", self._save)
        self.btn_next  = navlbl("Suivant ▶",          "#2a5a2a", self._go_next)
        self.btn_skip  = navlbl("⤵ 1ʳᵉ non annotée", "#3a3a3a", self._go_first_unannotated)
        self.btn_reset = navlbl("⟳ Réinitialiser",   "#6a2a2a", self._reset)
        for b in (self.btn_prev, self.btn_save, self.btn_next, self.btn_skip, self.btn_reset):
            b.pack(fill=tk.X, pady=2)

    def _tbtn(self, parent: tk.Frame, text: str, cmd) -> tk.Label:
        """Toggle label-button — Labels always honour bg/fg on macOS."""
        lb = tk.Label(parent, text=text, font=("Helvetica", 9, "bold"),
                      bg="#222222", fg="#666666", pady=6, padx=2,
                      cursor="hand2", relief=tk.FLAT)
        lb.bind("<Button-1>", lambda _e: cmd())
        return lb

    # ── State setters ─────────────────────────────────────────────────────────

    def _set_mode(self, m: str) -> None:
        self.mode = m
        self._refresh_panel()

    def _refresh_panel(self) -> None:
        def ab(btn: tk.Label, active: bool, active_bg: str) -> None:
            btn.config(bg=active_bg if active else "#222222",
                       fg="white"   if active else "#777777")

        ab(self.btn_write, self.mode == "write", "#2e7a2e")
        ab(self.btn_erase, self.mode == "erase", "#8a2a2a")

        if self.idx > 0:
            self.btn_prev.config(bg="#2a2a6a", fg="white",   cursor="hand2")
        else:
            self.btn_prev.config(bg="#1c1c1c", fg="#333333", cursor="")

        self.lbl_stats.config(text=f"Pointes : {len(self.points)}")

    # ── Image loading & rendering ──────────────────────────────────────────────

    def _load_image(self) -> None:
        path = self.images[self.idx]
        self.current_path = path
        self._dirty       = False

        self.points = []
        self.zoom   = 1.0
        self.pan_x  = 0.0
        self.pan_y  = 0.0

        name = path.name
        if name in self.annotations:
            self.points = [list(p) for p in self.annotations[name].get("points", [])]

        self.pil_img = Image.open(path).convert("RGB")
        self.img_w, self.img_h = self.pil_img.size

        screen_h   = max(self.root.winfo_screenheight() - 80, 600)
        self.scale = screen_h / self.img_h
        self._vp_h = screen_h
        self._vp_w = max(self.root.winfo_screenwidth() - PANEL_W, 100)

        self.lbl_file.config(text=path.name)
        self.lbl_idx.config(text=f"Image {self.idx + 1} / {len(self.images)}")

        self._redraw()
        self._refresh_panel()

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
        for x, y in self.points:
            cx, cy = x * s + px, y * s + py
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                    fill=COLOR_PT, outline="white", width=1)

    # ── Mouse events ──────────────────────────────────────────────────────────

    def _on_click(self, ev: tk.Event) -> None:
        s  = self.scale * self.zoom
        ix = max(0.0, min(float(self.img_w - 1), (ev.x - self.pan_x) / s))
        iy = max(0.0, min(float(self.img_h - 1), (ev.y - self.pan_y) / s))

        if self.mode == "write":
            self.points.append([ix, iy])
            self._dirty = True
            px, py = int(self.pan_x), int(self.pan_y)
            r = POINT_R
            self.canvas.create_oval(
                ix*s + px - r, iy*s + py - r,
                ix*s + px + r, iy*s + py + r,
                fill=COLOR_PT, outline="white", width=1)
            self._refresh_panel()
        else:
            if not self.points:
                return
            i = min(range(len(self.points)),
                    key=lambda k: dist2(ix, iy, self.points[k][0], self.points[k][1]))
            self.points.pop(i)
            self._dirty = True
            self._redraw()
            self._refresh_panel()

    # ── Zoom & pan ────────────────────────────────────────────────────────────

    def _on_wheel(self, ev: tk.Event) -> None:
        if ev.delta == 0:
            return
        factor = 1.15 if ev.delta > 0 else (1.0 / 1.15)
        self._do_zoom(factor, ev.x, ev.y)

    def _on_magnify(self, ev: tk.Event) -> None:
        """macOS pinch gesture."""
        factor = 1.0 + ev.delta
        if factor > 0.05:
            self._do_zoom(factor, ev.x, ev.y)

    def _do_zoom(self, factor: float, cx: float, cy: float) -> None:
        """Apply zoom centered on canvas point (cx, cy)."""
        new_zoom = max(1.0, min(_MAX_ZOOM, self.zoom * factor))
        self.pan_x = cx - (cx - self.pan_x) * new_zoom / self.zoom
        self.pan_y = cy - (cy - self.pan_y) * new_zoom / self.zoom
        self.zoom  = new_zoom
        self._clamp_pan()
        self._redraw()

    def _clamp_pan(self) -> None:
        """Keep the image within its viewport."""
        vp_w     = getattr(self, "_vp_w", int(self.img_w * self.scale))
        vp_h     = getattr(self, "_vp_h", int(self.img_h * self.scale))
        zoomed_w = self.img_w * self.scale * self.zoom
        zoomed_h = self.img_h * self.scale * self.zoom
        self.pan_x = (0.0 if zoomed_w <= vp_w
                      else max(vp_w - zoomed_w, min(0.0, self.pan_x)))
        self.pan_y = (0.0 if zoomed_h <= vp_h
                      else max(vp_h - zoomed_h, min(0.0, self.pan_y)))

    def _on_pan_start(self, ev: tk.Event) -> None:
        self._pan_anchor = (ev.x, ev.y)
        self.canvas.config(cursor="fleur")

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

    # ── Serialization ─────────────────────────────────────────────────────────

    def _commit(self) -> None:
        """Write current image's annotations to the in-memory dict."""
        self.annotations[self.current_path.name] = {
            "points": [list(p) for p in self.points]
        }

    def _flush(self) -> None:
        """Persist the in-memory annotations dict to disk."""
        ANN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ANN_FILE, "w") as f:
            json.dump(self.annotations, f, indent=2, ensure_ascii=False)
        self._dirty = False

    def _save(self) -> None:
        self._commit()
        self._flush()
        messagebox.showinfo("Sauvegardé",
                            f"Annotations sauvegardées dans\n{ANN_FILE.name}")

    def _ask_nav_save(self) -> Optional[bool]:
        """Prompt to save before navigating away. True=save, False=skip, None=cancel."""
        return messagebox.askyesnocancel(
            "Modifications non sauvegardées",
            f"Sauvegarder {self.current_path.name} avant de continuer ?",
        )

    # ── Navigation ────────────────────────────────────────────────────────────

    def _go_prev(self) -> None:
        if self.idx == 0:
            return
        if self._dirty:
            resp = self._ask_nav_save()
            if resp is None:
                return
            if resp:
                self._commit()
                self._flush()
        self.idx -= 1
        self._load_image()

    def _go_next(self) -> None:
        if self._dirty:
            resp = self._ask_nav_save()
            if resp is None:
                return
            if resp:
                self._commit()
                self._flush()
        if self.idx >= len(self.images) - 1:
            messagebox.showinfo("Terminé",
                                "Toutes les images ont été parcourues.\n"
                                f"Annotations dans {ANN_FILE.name}")
            return
        self.idx += 1
        self._load_image()

    def _go_first_unannotated(self) -> None:
        """Jump to the first image with no annotation entry yet."""
        if self._dirty:
            resp = self._ask_nav_save()
            if resp is None:
                return
            if resp:
                self._commit()
                self._flush()
        for i, path in enumerate(self.images):
            if path.name not in self.annotations:
                self.idx = i
                self._load_image()
                return
        messagebox.showinfo("Tout annoté",
                            "Toutes les images ont déjà une annotation.")

    def _reset(self) -> None:
        if not messagebox.askyesno("Réinitialiser",
                                   "Supprimer tous les points de cette image ?"):
            return
        self.points.clear()
        self._dirty = True
        self._redraw()
        self._refresh_panel()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    images = collect_images()
    if not images:
        print("Aucune image trouvée.")
        return

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
