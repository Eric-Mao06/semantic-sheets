"""Threshold calibration for semantic scores.

The planner writes a question without seeing a single score, so any fixed true_min/false_max it picks is a guess
about a distribution it has not observed. Once a stage is fully scored we *have* the distribution, and the cut can
be placed where the scores actually separate instead of where the planner assumed they would.

The rule is deliberately unsupervised and parameter-light so it cannot be tuned to a benchmark:

* Otsu's method on a 100-bin histogram of the scores. It picks the cut that maximises between-class variance,
  i.e. the cut between the two clusters if there are two. No labels, no per-dataset knobs.
* If Otsu's cut lands outside CLAMP the scores are essentially one cluster (everything low or everything high);
  the cut is clamped so the whole population falls on one side rather than splitting noise.
* Fewer than MIN_ROWS scored rows: keep the planner's fixed thresholds (too little data to see a distribution).
* Rows within FLAG_MARGIN of the cut still get a value but are flagged in <name>.near so a filter's review view
  can list them for a spot check without dropping them from the output.

These constants were fixed before the calibrated benchmark run and must not be changed to chase a result; if a
future change is needed, bump VERSION so runs remain comparable."""
from __future__ import annotations

from dataclasses import asdict, dataclass

VERSION = "otsu-v1"
BINS = 100
MIN_ROWS = 50
CLAMP = (0.20, 0.80)
FLAG_MARGIN = 0.10


@dataclass
class Calibration:
    mode: str                      # "auto" | "fixed" | "fallback" (auto requested, too few rows)
    cut: float                     # value = score >= cut
    flag_margin: float
    n_scored: int
    method: str = VERSION
    raw_cut: float | None = None   # Otsu's cut before clamping
    clamped: bool = False
    planner_true_min: float | None = None
    planner_false_max: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def otsu_cut(scores: list[float], bins: int = BINS) -> float:
    """Return the cut t (a bin edge in (0, 1)) maximising between-class variance of {s < t} vs {s >= t}.

    When several cuts tie (an empty gap between the clusters) the middle of the plateau is returned, so the cut
    sits in the gap rather than hugging one cluster."""
    if not scores:
        raise ValueError("otsu_cut needs at least one score")
    hist = [0] * bins
    for s in scores:
        i = int(min(max(s, 0.0), 1.0) * bins)
        hist[min(i, bins - 1)] += 1
    total = len(scores)
    centers = [(i + 0.5) / bins for i in range(bins)]
    sum_all = sum(h * c for h, c in zip(hist, centers))
    best = -1.0
    plateau: list[int] = []
    w0 = 0
    sum0 = 0.0
    for k in range(bins - 1):
        w0 += hist[k]
        sum0 += hist[k] * centers[k]
        w1 = total - w0
        if w0 == 0 or w1 == 0:
            continue
        mu0, mu1 = sum0 / w0, (sum_all - sum0) / w1
        sigma_b = w0 * w1 * (mu0 - mu1) ** 2
        if sigma_b > best + 1e-12:
            best, plateau = sigma_b, [k]
        elif abs(sigma_b - best) <= 1e-12:
            plateau.append(k)
    if not plateau:  # all scores in one bin
        k = next(i for i, h in enumerate(hist) if h)
        return (k + 1) / bins
    # plateau entries k mean "cut after bin k" -> edge (k + 1) / bins
    mid = plateau[len(plateau) // 2] if len(plateau) % 2 else (plateau[len(plateau) // 2 - 1] + plateau[len(plateau) // 2]) / 2
    return round((mid + 1) / bins, 4)


def calibrate(scores: list[float], planner_true_min: float, planner_false_max: float, mode: str = "auto") -> Calibration:
    """Decide the cut for one question given every scored row of the stage."""
    n = len(scores)
    if mode != "auto":
        return Calibration("fixed", planner_true_min, 0.0, n, method="fixed", planner_true_min=planner_true_min, planner_false_max=planner_false_max)
    if n < MIN_ROWS:
        return Calibration("fallback", planner_true_min, 0.0, n, method="fixed", planner_true_min=planner_true_min, planner_false_max=planner_false_max)
    raw = otsu_cut(scores)
    cut = min(max(raw, CLAMP[0]), CLAMP[1])
    return Calibration("auto", cut, FLAG_MARGIN, n, raw_cut=raw, clamped=cut != raw, planner_true_min=planner_true_min, planner_false_max=planner_false_max)


def decide(score: float, cal: Calibration) -> tuple[bool, bool]:
    """(value, near) for one score under a calibration."""
    return score >= cal.cut, abs(score - cal.cut) <= cal.flag_margin + 1e-12
