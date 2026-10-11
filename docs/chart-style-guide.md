# Home Automation Chart Style Guide

Use this guide for all future Matplotlib / pyplot charts in the home automation workspace.

## Design goal

Charts should feel **clean, modern, calm, and technical**.

Priorities:

1. Readability
2. Consistency
3. Minimal visual noise
4. A clear connection to the existing navy / blue / mint visual identity

## Color system

```python
COLORS = {
    "background": "#0B1220",
    "surface": "#131F31",
    "primary": "#4B8FE8",
    "secondary": "#7AB8F5",
    "accent": "#67DDD1",
    "muted": "#718399",
    "text": "#E7EDF5",
    "text_secondary": "#9BAABC",
    "grid": "#27364A",
    "warning": "#F0A86B",
    "error": "#E76F73",
}
```

Semantic use:

- **Blue** = normal / primary data
- **Mint** = active, selected, important, positive, or noteworthy
- **Slate** = historical / comparison / context
- **Amber** = warning
- **Coral** = error / failure

Do not use rainbow palettes unless categories genuinely require distinct colors.

## Core rules

- Dark navy background.
- Flat chart colors; avoid gradients and glow inside charts.
- Use mint sparingly for emphasis.
- Prefer subtle **horizontal grid lines only**.
- Remove top/right/left/bottom spines unless structurally necessary.
- Avoid chart borders, shadows, 3D effects, decorative backgrounds, and excessive markers.
- Use generous whitespace.
- Keep titles short and sentence case.
- Use compact, human-readable numbers and timestamps.
- Legends should be minimal and frameless.
- The data should always be visually stronger than the grid or axes.

## Visual hierarchy

Use at most three emphasis levels:

1. **Primary** — cobalt blue
2. **Context** — soft blue or muted slate
3. **Highlight** — mint

Example: today uses `primary`, yesterday uses `muted`, an important event uses `accent`.

## Chart selection

- Time series / sensor history → **line chart**
- Device / room comparison → **horizontal bar chart**
- Current vs target → **bar / bullet-style chart**
- Distribution → **histogram**
- Relationship between metrics → **scatter**
- On/off or automation states → **step chart / timeline**
- Trigger events → **line chart + event markers**
- Part-to-whole → prefer **bar**; use pie/donut sparingly

## Line charts

- Default width: `2.2–2.5`.
- Avoid markers for dense time series.
- Use small markers only for sparse data.
- Prefer horizontal grid lines only.
- Historical/reference lines should be thinner and/or lower alpha.

## Bar charts

- Use a single primary color by default.
- Highlight only the selected / important bar with mint.
- Prefer horizontal bars for 6+ categories.
- Keep bar widths visually substantial and uncluttered.

## Titles and labels

Preferred pattern:

```text
Energy consumption
Last 24 hours · Living room
```

- Main title: ~15 pt, semibold.
- Subtitle: ~9 pt, muted.
- Axis labels: ~10 pt, muted.
- Avoid long, all-caps titles.

## Number formatting

Prefer `14.6 kW`, `7.4 Wh`, `23 °C`, and `12:00`.
Avoid unnecessary precision such as `14563.392 W`, `23.000 °C`, and full ISO timestamps unless required.

## Recommended figure sizes

```python
figsize = (10, 5)  # General
figsize = (8, 4)   # Compact dashboard card
figsize = (12, 5)  # Wide time series
figsize = (7, 7)   # Square overview
```

## Default Matplotlib setup

Use the shared `home_energy.charts.chart_style()` context instead of changing global settings:

```python
import matplotlib.pyplot as plt
from home_energy.charts import chart_style

with chart_style():
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot([0, 1], [1, 2])
    # Horizontal grids, frameless legends, hidden spines, and typography
    # come from the shared style. Add domain-specific titles and labels here.
```

Defaults: sans-serif 11 pt, title 15 pt semibold, line width 2.3,
grid alpha 0.55 / width 0.8, figure 120 dpi, PNG 180 dpi with tight bounding box.
Figure, axes, and saved backgrounds use navy. Text and grid use the palette above.

## Agent instruction

> Create calm, dark, modern dashboard charts using deep navy backgrounds, cobalt blue as the primary data color, mint only for meaningful highlights or active states, muted slate for context, subtle horizontal grids, generous whitespace, minimal labels, and no decorative chart elements. Optimize first for immediate readability, then aesthetics.

## Established price-chart baseline

The supplied price script is the baseline for price charts: blue smoothed prices,
slate comparison profile, and coral expensive-price segments. This preserves its
specific highlighting convention. Generic charts default meaningful highlights to mint;
other warnings use amber. Thresholds are evaluated against raw prices, and negative
prices and DST days remain visible. The reference profile is supplied context with
unknown historical provenance.
