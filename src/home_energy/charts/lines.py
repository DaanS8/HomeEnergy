"""Domain-independent line charts, highlighted segments, and PNG saving."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

from .style import COLORS, chart_style


@dataclass(frozen=True)
class LineSeries:
    x: Sequence
    y: Sequence
    label: str | None = None
    role: str = "primary"
    linewidth: float | None = None
    alpha: float | None = None


@dataclass(frozen=True)
class HighlightSegment:
    start_x: object
    end_x: object
    start_y: float
    end_y: float
    label: str | None = None
    role: str = "accent"


def compact_number(value: float) -> str:
    for scale, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= scale:
            return f"{value / scale:.3g}{suffix}"
    return f"{value:.3g}"


def build_line_chart(series: Sequence[LineSeries], *, title: str, subtitle: str = "",
                     ylabel: str = "", xlabel: str = "", figsize=(10, 5),
                     highlights: Sequence[HighlightSegment] = (),
                     xticks: Sequence | None = None, xticklabels: Sequence[str] | None = None,
                     y_formatter: Callable[[float], str] = compact_number,
                     xlim: tuple | None = None) -> Figure:
    """Return a caller-owned Agg figure without selecting a global backend.

    x accepts numeric or aware datetime coordinates. None/NaN y values break
    lines naturally, so usage gaps do not turn into fabricated zero readings.
    """
    if not series or any(not len(line.x) or len(line.x) != len(line.y) for line in series):
        raise ValueError("Provide nonempty line series with equal x/y lengths.")
    if xticklabels is not None and (xticks is None or len(xticks) != len(xticklabels)):
        raise ValueError("Tick positions and labels must have equal lengths.")
    if any(item.role not in COLORS for item in [*series, *highlights]):
        raise ValueError("Unknown chart color role.")
    with chart_style():
        figure = Figure(figsize=figsize)
        FigureCanvasAgg(figure)
        ax = figure.add_subplot()
        for line in series:
            context = line.role in ("muted", "secondary")
            ax.plot(line.x, [float(v) if v is not None else float("nan") for v in line.y],
                    color=COLORS[line.role], label=line.label,
                    linewidth=line.linewidth if line.linewidth is not None else (1.7 if context else 2.3),
                    alpha=line.alpha if line.alpha is not None else (0.7 if context else 1),
                    zorder=1 if context else 2)
        for segment in highlights:
            ax.plot([segment.start_x, segment.end_x], [segment.start_y, segment.end_y],
                    color=COLORS[segment.role], linewidth=2.8, label=segment.label, zorder=3)
        ax.set_title(title, loc="left", pad=34 if subtitle else 16)
        if subtitle:
            ax.text(0, 1.035, subtitle, transform=ax.transAxes, fontsize=9,
                    color=COLORS["text_secondary"], ha="left", va="bottom")
        ax.set_ylabel(ylabel)
        ax.set_xlabel(xlabel)
        if xticks is not None:
            ax.set_xticks(xticks)
            if xticklabels is not None:
                ax.set_xticklabels(xticklabels)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: y_formatter(value)))
        ax.grid(True, axis="y")
        ax.grid(False, axis="x")
        ax.margins(x=0.01, y=0.12)
        if xlim is not None:
            ax.set_xlim(*xlim)
        handles, labels = ax.get_legend_handles_labels()
        if handles:
            ax.legend(handles, labels, loc="upper left", ncol=min(3, len(handles)), fontsize=9)
        figure.tight_layout(pad=1.5)
        return figure


def save_png(figure: Figure, path: str | Path) -> Path:
    """Save a styled PNG, creating its parent directory; caller owns cleanup."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with chart_style():
        figure.savefig(path, format="png", dpi=180, bbox_inches="tight", facecolor=COLORS["background"])
    return path
