# Wheat Tip Annotator

Tool for annotating wheat tip points in images using a graphical interface.

## Setup

### Requirements
- Python 3.8+
- Libraries: `pillow`, `tkinter` (usually included with Python)

### Installation

```bash
pip install pillow
```

On Linux, you may need to install tkinter separately:
```bash
# Ubuntu/Debian
sudo apt-get install python3-tk

# Fedora
sudo dnf install python3-tkinter
```

## Usage

```bash
python annotate.py
```

## Controls

| Action | Command |
|--------|---------|
| **Place point** | Left-click (Mode Écrire) |
| **Delete point** | Left-click on closest point (Mode Effacer) |
| **Zoom** | Scroll wheel or pinch gesture |
| **Pan** | Right-click drag or middle-button drag |

## Navigation

- **◀ Précédent** — Go to previous image
- **Suivant ▶** — Go to next image
- **⤵ 1ʳᵉ non annotée** — Jump to first unannotated image
- **💾 Sauvegarder** — Save annotations to `annotations.json`
- **⟳ Réinitialiser** — Clear all points on current image

## Output

Annotations are saved to `annotations.json` with point coordinates for each image.

## OLD_annotate.py

This is the script to annotate wheat and pea from the linear meters of the orthomosaic (just put here as a bigger annotator with more possibilities).
