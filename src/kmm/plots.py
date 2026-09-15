"""Charts for the calibration analysis.

Two figures, both saved as PNG for the Phase 4 markdown report.

Palette note: the three series colours are slots 1 to 3 of the reference
categorical palette, which is the documented set that clears the all-pairs
colourblind separation gate (worst CVD dE 9.2 light). Scatter is an all-pairs
form, so the three-slot cap applies and a fourth series would have to fold into
"other" or become its own facet rather than take slot 4.

Identity never rests on colour alone here: every series carries a distinct marker
and a legend entry, and the reference diagonal is dashed.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # render to file, no display needed
import matplotlib.pyplot as plt

from .calibration import Observation, brier_decomposition, by_horizon, reliability_table

SERIES_1 = "#2a78d6"   # blue
SERIES_2 = "#eb6834"   # orange
SERIES_3 = "#1baf7a"   # aqua
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#8a8880"
SURFACE = "#fcfcfb"
GRID = "#e4e3de"


def _style(ax) -> None:
    """Recessive grid and axes, so the marks carry the attention."""
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_SECONDARY, labelsize=9)


def reliability_diagram(obs: list[Observation], path: Path, title: str = "",
                        n_bins: int = 10) -> Path:
    """Reliability curve over a histogram of how much data sits behind each bin.

    The histogram is not decoration. A reliability curve without sample sizes is
    close to unreadable: a bin holding four observations will wander far from the
    diagonal through noise alone, and shown without its n it looks exactly like
    evidence of miscalibration. The error bars carry the same information, the
    histogram makes it impossible to miss.
    """
    table = reliability_table(obs, n_bins)
    if not table:
        raise ValueError("no observations to plot")

    fig, (ax, ax_n) = plt.subplots(
        2, 1, figsize=(7.0, 7.2), height_ratios=[3, 1], sharex=True,
        gridspec_kw={"hspace": 0.08},
    )
    fig.patch.set_facecolor(SURFACE)

    # Reference diagonal: dashed and neutral, so it reads as the ideal rather
    # than as another series competing for attention.
    ax.plot([0, 1], [0, 1], linestyle="--", linewidth=1.5, color=INK_MUTED,
            label="perfect calibration", zorder=1)

    xs = [b.mean_pred for b in table]
    ys = [b.observed_freq for b in table]
    lo_err = [max(0.0, b.observed_freq - b.ci_lo) for b in table]
    hi_err = [max(0.0, b.ci_hi - b.observed_freq) for b in table]

    ax.errorbar(xs, ys, yerr=[lo_err, hi_err], fmt="o", markersize=8, linewidth=2,
                color=SERIES_1, ecolor=SERIES_1, elinewidth=1.5, capsize=3,
                markeredgecolor=SURFACE, markeredgewidth=1.5,
                label="observed frequency (95% Wilson)", zorder=3)

    # Selective direct labels: only the bins where the price sits outside the
    # observed interval, which are the only ones making a claim.
    for b in table:
        if b.significant:
            ax.annotate(f"{b.gap:+.2f}", (b.mean_pred, b.observed_freq),
                        textcoords="offset points", xytext=(9, -3),
                        fontsize=8, color=INK_SECONDARY)

    ax.set_ylabel("resolved YES (fraction)", color=INK_SECONDARY, fontsize=10)
    ax.set_xlim(0, 1)
    ax.set_ylim(-0.03, 1.03)
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK_SECONDARY)
    if title:
        ax.set_title(title, color=INK_PRIMARY, fontsize=12, loc="left", pad=12)
    _style(ax)

    widths = [(b.hi - b.lo) * 0.85 for b in table]
    ax_n.bar([b.mean_pred for b in table], [b.n for b in table], width=widths,
             color=SERIES_1, alpha=0.45, edgecolor=SURFACE, linewidth=2, zorder=2)
    ax_n.set_xlabel("implied probability (mid of YES bid and ask)",
                    color=INK_SECONDARY, fontsize=10)
    ax_n.set_ylabel("observations", color=INK_SECONDARY, fontsize=10)
    _style(ax_n)

    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return path


def horizon_diagram(obs: list[Observation], path: Path, title: str = "") -> Path:
    """Brier and its components against time to resolution.

    Brier almost always falls as resolution approaches, which on its own says
    nothing: the outcome is becoming obvious. The informative line is RESOLUTION,
    which measures how far the market moved away from the base rate while still
    being right. Resolution rising early means the market was anticipating rather
    than merely catching up.
    """
    rows = by_horizon(obs)
    if not rows:
        raise ValueError("no observations to plot")

    labels = [label for label, _ in rows]
    x = range(len(rows))
    fig, ax = plt.subplots(figsize=(7.0, 4.2))
    fig.patch.set_facecolor(SURFACE)

    series = [
        ("Brier (lower better)", [p.brier for _, p in rows], SERIES_1, "o"),
        ("resolution (higher better)", [p.resolution for _, p in rows], SERIES_3, "s"),
        ("excess reliability (0 = calibrated)", [p.reliability_excess for _, p in rows],
         SERIES_2, "^"),
    ]
    for label, ys, colour, marker in series:
        ax.plot(x, ys, marker=marker, markersize=8, linewidth=2, color=colour,
                markeredgecolor=SURFACE, markeredgewidth=1.5, label=label, zorder=3)

    # Sample size per bucket, so a jumpy line cannot be mistaken for a trend.
    # Folded into the tick label rather than annotated near the axis, where it
    # collided with the ticks themselves.
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"{label}\nn={p.n}" for label, (_, p) in zip(labels, rows)])
    ax.set_xlabel("time to resolution", color=INK_SECONDARY, fontsize=10, labelpad=10)
    ax.set_ylabel("score", color=INK_SECONDARY, fontsize=10)
    ax.axhline(0, color=INK_MUTED, linewidth=1, linestyle="--", zorder=1)
    ax.legend(loc="upper right", frameon=False, fontsize=9, labelcolor=INK_SECONDARY)
    if title:
        ax.set_title(title, color=INK_PRIMARY, fontsize=12, loc="left", pad=12)
    _style(ax)

    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return path


def reliability_table_markdown(obs: list[Observation], n_bins: int = 10) -> str:
    """The same numbers as a table, so the chart is never the only way in.

    Required rather than optional: several palette slots sit below 3:1 contrast on
    a light surface, and the relief rule for that is a visible table view.
    """
    lines = [
        "| bin | n | mean price | resolved yes | 95% CI | gap |",
        "|---|---|---|---|---|---|",
    ]
    for b in reliability_table(obs, n_bins):
        flag = " \\*" if b.significant else ""
        lines.append(
            f"| {b.lo:.1f} to {b.hi:.1f} | {b.n} | {b.mean_pred:.3f} | {b.observed_freq:.3f} | "
            f"[{b.ci_lo:.3f}, {b.ci_hi:.3f}] | {b.gap:+.3f}{flag} |"
        )
    parts = brier_decomposition(obs)
    lines.append("")
    lines.append(f"Brier {parts.brier:.4f}, excess reliability {parts.reliability_excess:+.4f}, "
                 f"resolution {parts.resolution:.4f}, n={parts.n}.")
    return "\n".join(lines)
