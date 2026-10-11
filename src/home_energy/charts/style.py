"""Scoped home automation chart theme; importing it does not change rcParams."""

from contextlib import contextmanager
from types import MappingProxyType

import matplotlib


COLORS = MappingProxyType({
    "background": "#0B1220", "surface": "#131F31",
    "primary": "#4B8FE8", "secondary": "#7AB8F5", "accent": "#67DDD1",
    "muted": "#718399", "text": "#E7EDF5", "text_secondary": "#9BAABC",
    "grid": "#27364A", "warning": "#F0A86B", "error": "#E76F73",
})

STYLE = {
    "figure.facecolor": COLORS["background"],
    "axes.facecolor": COLORS["background"],
    "savefig.facecolor": COLORS["background"],
    "font.family": "sans-serif", "font.size": 11,
    "axes.titlesize": 15, "axes.titleweight": "semibold",
    "axes.titlecolor": COLORS["text"], "axes.labelsize": 10,
    "axes.labelcolor": COLORS["text_secondary"],
    "xtick.color": COLORS["text_secondary"], "ytick.color": COLORS["text_secondary"],
    "xtick.labelsize": 10, "ytick.labelsize": 10,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.spines.left": False, "axes.spines.bottom": False,
    "axes.grid": True, "axes.grid.axis": "y",
    "grid.color": COLORS["grid"], "grid.alpha": 0.55, "grid.linewidth": 0.8,
    "legend.frameon": False, "legend.labelcolor": COLORS["text"],
    "lines.linewidth": 2.3, "figure.dpi": 120,
    "savefig.dpi": 180, "savefig.bbox": "tight",
}


@contextmanager
def chart_style():
    """Apply shared defaults to custom Matplotlib plots, then restore settings."""
    with matplotlib.rc_context(STYLE):
        yield
