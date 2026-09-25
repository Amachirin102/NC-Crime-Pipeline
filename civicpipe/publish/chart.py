"""Static PNG of the monthly trend (for the README and for newsroom CMS embeds).

Two panels, not one dual-axis chart: violent and property crime differ by ~5x
in volume, and putting them on one axis would flatten the smaller series.
Months still inside the settle window are drawn as hollow markers and
labeled preliminary, because they will rise as late reports arrive.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mt  # noqa: E402
import pandas as pd  # noqa: E402

INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e5e0", "#fcfcfb"
SERIES = {"violent": "#2a78d6", "property": "#eb6834"}
TITLES = {"violent": "Violent crime", "property": "Property crime"}


def trend_png(m: pd.DataFrame, out: Path, source_note: str) -> Path:
    x = m["month"].dt.to_timestamp()
    fig, axes = plt.subplots(2, 1, figsize=(9, 6.4), sharex=True, facecolor=SURFACE)
    for ax, col in zip(axes, ["violent", "property"]):
        ax.set_facecolor(SURFACE)
        s, pre = m["settled"], ~m["settled"]
        ax.plot(x[s], m.loc[s, col], color=SERIES[col], lw=2, solid_capstyle="round", solid_joinstyle="round")
        if pre.any():
            ax.plot(x[pre], m.loc[pre, col], ls="none", marker="o", ms=6, mfc=SURFACE,
                    mec=SERIES[col], mew=1.5)
            ax.annotate("preliminary", (x[pre].iloc[-1], m.loc[pre, col].iloc[-1]),
                        xytext=(6, -14), textcoords="offset points", fontsize=8, color=INK2)
        last = m[s].iloc[-1]
        ax.plot([last["month"].to_timestamp()], [last[col]], "o", color=SERIES[col], ms=6)
        ax.annotate(f"{int(last[col]):,}", (last["month"].to_timestamp(), last[col]),
                    xytext=(6, 6), textcoords="offset points", fontsize=9, color=INK)
        ax.set_title(f"{TITLES[col]} reports per month", loc="left", fontsize=11, color=INK, pad=6)
        ax.set_ylim(0, m[col].max() * 1.18)
        ax.yaxis.set_major_formatter(mt.FuncFormatter(lambda v, _: f"{v:,.0f}"))
        ax.grid(axis="y", color=GRID, lw=1)
        for sp in ("top", "right", "left"):
            ax.spines[sp].set_visible(False)
        ax.spines["bottom"].set_color(GRID)
        ax.tick_params(colors=INK2, labelsize=9, length=0)
    fig.suptitle("Charlotte-Mecklenburg Police: reported crime by month", x=0.02, ha="left",
                 fontsize=13, fontweight="bold", color=INK)
    fig.text(0.02, 0.01, source_note, fontsize=7.5, color=INK2, ha="left", va="bottom", wrap=True)
    fig.tight_layout(rect=(0, 0.05, 1, 0.97))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=160)
    plt.close(fig)
    return out
