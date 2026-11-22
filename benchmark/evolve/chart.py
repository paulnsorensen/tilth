"""An SVG chart of the evolve benchmark log, drawn with the standard library only.

The top panel shows, per run, the seed and winner dev means on the score scale, where 1.0 is cost-per-correct
parity with the built-in-tools baseline. A ring marks a run that opened a draft PR. The bottom panel shows the
paired test delta (winner minus seed) with its 95% CI. The same entries give the same bytes.
"""

import math
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

WIDTH, HEIGHT = 800, 440
LEFT, RIGHT = 56, 88
SCORE_TOP, SCORE_BOTTOM = 72, 262
DELTA_TOP, DELTA_BOTTOM = 306, 380
TICK_LABEL_Y = 398
NOTE_Y = 426
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"

# The reference data-viz palette: categorical slots 1 and 2, the status "good" green, and the chart chrome.
THEMES = {
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "secondary": "#52514e", "muted": "#6f6d68",
              "grid": "#e1e0d9", "axis": "#c3c2b7", "winner": "#2a78d6", "seed": "#eb6834", "pr": "#0ca30c"},
    "dark": {"surface": "#1a1a19", "ink": "#f0efec", "secondary": "#c3c2b7", "muted": "#a3a199",
             "grid": "#2c2c2a", "axis": "#383835", "winner": "#3987e5", "seed": "#d95926", "pr": "#0ca30c"},
}
TITLE = "tilth evolve runs: score against the built-in-tools baseline"
NOTE = "Score = pass rate × baseline cost ÷ candidate cost per task, dev-split mean. Above 1.0 is cheaper per correct."


def _n(value: float) -> str:
    """A coordinate with one decimal and no negative zero, so the output is byte-stable."""
    text = f"{value:.1f}"
    return "0.0" if text == "-0.0" else text


def _text(x: float, y: float, body: str, fill: str, *, size: int = 12, anchor: str = "start",
          weight: str = "normal") -> str:
    return (f'<text x="{_n(x)}" y="{_n(y)}" fill="{fill}" font-size="{size}" text-anchor="{anchor}" '
            f'font-weight="{weight}">{escape(body)}</text>')


def _line(x1: float, y1: float, x2: float, y2: float, stroke: str, *, width: float = 1, dash: str = "") -> str:
    extra = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{_n(x1)}" y1="{_n(y1)}" x2="{_n(x2)}" y2="{_n(y2)}" stroke="{stroke}" '
            f'stroke-width="{width}"{extra}/>')


def nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    """Round tick values that cover ``[low, high]`` with at most ``count + 1`` steps."""
    span = high - low if high > low else 1.0
    base = 10 ** math.floor(math.log10(span / count))
    step = next(base * factor for factor in (1, 2, 2.5, 5, 10) if span / (base * factor) <= count)
    first = math.floor(low / step + 1e-9) * step
    ticks = []
    value = first
    while value < high + step - 1e-9:
        ticks.append(round(value, 10))
        value += step
    return ticks


def _when(entry: dict) -> datetime:
    return datetime.strptime(entry["recorded_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _x_positions(times: list[datetime]) -> list[float]:
    left, right = LEFT + 24, WIDTH - RIGHT - 24
    start, end = times[0], times[-1]
    span = (end - start).total_seconds()
    if span <= 0:
        return [(left + right) / 2 for _ in times]
    return [left + (right - left) * (when - start).total_seconds() / span for when in times]


def _time_ticks(times: list[datetime], xs: list[float]) -> list[tuple[float, str]]:
    """Up to five time ticks over the run range; a range under four days shows the hour too."""
    start, end = times[0], times[-1]
    span = (end - start).total_seconds()
    form = "%Y-%m-%d" if span >= 4 * 86400 else "%Y-%m-%d %H:%M"
    if span <= 0:
        return [(xs[0], start.strftime(form))]
    left, right = xs[0], xs[-1]
    ticks, seen = [], set()
    for index in range(5):
        moment = datetime.fromtimestamp(start.timestamp() + span * index / 4, tz=timezone.utc)
        label = moment.strftime(form)
        if label not in seen:
            seen.add(label)
            ticks.append((left + (right - left) * index / 4, label))
    return ticks


def _scale(low: float, high: float, top: float, bottom: float):
    def to_y(value: float) -> float:
        return bottom - (value - low) / (high - low) * (bottom - top)
    return to_y


def _series(points: list[tuple[float, float | None]], to_y, color: str, surface: str, name: str,
            run_ids: list[str]) -> list[str]:
    """A 2px line through consecutive known points, broken at a run without a value, and one dot per point."""
    parts, segment = [], []
    for x, value in [*points, (0.0, None)]:
        if value is None:
            if len(segment) > 1:
                parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" '
                             f'points="{" ".join(f"{_n(px)},{_n(py)}" for px, py in segment)}"/>')
            segment = []
            continue
        segment.append((x, to_y(value)))
    for (x, value), run_id in zip(points, run_ids):
        if value is not None:
            parts.append(f'<circle cx="{_n(x)}" cy="{_n(to_y(value))}" r="4.5" fill="{color}" stroke="{surface}" '
                         f'stroke-width="2"><title>{escape(f"{run_id}: {name} dev mean {value:.3f}")}</title>'
                         "</circle>")
    return parts


def _frame(theme: dict, body: list[str]) -> str:
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
            f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" font-family={quoteattr(FONT)}>')
    return "\n".join([
        head, f"<title>{escape(TITLE)}</title>",
        f'<rect width="{WIDTH}" height="{HEIGHT}" rx="8" fill="{theme["surface"]}"/>',
        _text(16, 26, TITLE, theme["ink"], size=15, weight="600"), *body, "</svg>",
    ]) + "\n"


def _legend(theme: dict) -> list[str]:
    parts, x = [], 16.0
    for label, color in (("seed dev mean", theme["seed"]), ("winner dev mean", theme["winner"])):
        parts += [_line(x, 46, x + 18, 46, color, width=2),
                  f'<circle cx="{_n(x + 9)}" cy="46" r="4.5" fill="{color}" stroke="{theme["surface"]}" '
                  'stroke-width="2"/>',
                  _text(x + 24, 50, label, theme["secondary"])]
        x += 24 + 7 * len(label) + 20
    parts += [f'<circle cx="{_n(x + 9)}" cy="46" r="7" fill="none" stroke="{theme["pr"]}" stroke-width="2"/>',
              _text(x + 22, 50, "PR: draft PR opened", theme["secondary"])]
    x += 22 + 7 * len("PR: draft PR opened") + 20
    parts += [_line(x, 46, x + 18, 46, theme["muted"], dash="4 4"),
              _text(x + 24, 50, "parity 1.0", theme["secondary"])]
    return parts


def _y_axis(ticks: list[float], to_y, theme: dict, *, sign: str = "") -> list[str]:
    """Gridlines and labels; the labels carry as many decimals as the tick step needs."""
    step = ticks[1] - ticks[0] if len(ticks) > 1 else 1.0
    decimals = 0
    while decimals < 6 and abs(step * 10 ** decimals - round(step * 10 ** decimals)) > 1e-6:
        decimals += 1
    parts = []
    for tick in ticks:
        y = to_y(tick)
        label = format(0.0 if abs(tick) < 1e-12 else tick, f"{sign}.{decimals}f")
        parts += [_line(LEFT, y, WIDTH - RIGHT, y, theme["grid"]),
                  _text(LEFT - 8, y + 4, label, theme["muted"], size=11, anchor="end")]
    return parts


def _direct_labels(labels: list[tuple[float, float, str, str]]) -> list[str]:
    """Series names right of their last point, pushed apart when they sit closer than 14px."""
    labels = sorted(labels, key=lambda label: label[1])
    for index in range(1, len(labels)):
        x, y, text, color = labels[index]
        if y - labels[index - 1][1] < 14:
            labels[index] = (x, labels[index - 1][1] + 14, text, color)
    return [_text(x + 10, y + 4, text, color, weight="600") for x, y, text, color in labels]


def render(entries: list[dict], theme_name: str) -> str:
    """The chart of ``entries`` in the ``light`` or ``dark`` theme, as SVG text; runs sort by time, then run id."""
    theme = THEMES[theme_name]
    if not entries:
        return _frame(theme, [
            _line(LEFT, SCORE_BOTTOM, WIDTH - RIGHT, SCORE_BOTTOM, theme["axis"]),
            _text(WIDTH / 2, (SCORE_TOP + SCORE_BOTTOM) / 2, "No evolve runs yet", theme["secondary"], size=16,
                  anchor="middle"),
            _text(16, NOTE_Y, NOTE, theme["muted"], size=11),
        ])
    entries = sorted(entries, key=lambda entry: (entry["recorded_at"], entry["run_id"]))
    times = [_when(entry) for entry in entries]
    xs = _x_positions(times)
    run_ids = [entry["run_id"] for entry in entries]
    seed = [entry.get("seed_dev_mean") for entry in entries]
    best = [entry.get("best_dev_mean") for entry in entries]
    deltas = [entry.get("paired_test_delta") for entry in entries]

    scores = [value for value in (*seed, *best) if value is not None]
    score_ticks = nice_ticks(min(0.0, *scores), max(1.2, *(value * 1.05 for value in scores)))
    to_score = _scale(score_ticks[0], score_ticks[-1], SCORE_TOP, SCORE_BOTTOM)
    spread = [value for delta in deltas if delta for value in (delta["low"], delta["high"], delta["point"])]
    reach = max([0.1, *(abs(value) * 1.1 for value in spread)])
    delta_ticks = nice_ticks(-reach, reach, 2)
    to_delta = _scale(delta_ticks[0], delta_ticks[-1], DELTA_TOP, DELTA_BOTTOM)

    body = _legend(theme)
    body += _y_axis(score_ticks, to_score, theme)
    body += [_line(LEFT, to_score(1.0), WIDTH - RIGHT, to_score(1.0), theme["muted"], width=1.5, dash="4 4"),
             _text(LEFT + 6, to_score(1.0) - 6, "parity 1.0", theme["muted"], size=11)]
    body += _series(list(zip(xs, seed)), to_score, theme["seed"], theme["surface"], "seed", run_ids)
    body += _series(list(zip(xs, best)), to_score, theme["winner"], theme["surface"], "winner", run_ids)
    for x, entry, seed_value, best_value in zip(xs, entries, seed, best):
        anchor = best_value if best_value is not None else seed_value
        if entry.get("outcome") == "pr_opened" and anchor is not None:
            y = to_score(anchor)
            tip = f"{entry['run_id']}: draft PR opened"
            body += [f'<circle cx="{_n(x)}" cy="{_n(y)}" r="9" fill="none" stroke="{theme["pr"]}" '
                     f'stroke-width="2"><title>{escape(tip)}</title></circle>',
                     _text(x, y - 14, "PR", theme["ink"], size=11, anchor="middle", weight="600")]
    labels = []
    for name, values in (("seed", seed), ("winner", best)):
        last = [(x, value) for x, value in zip(xs, values) if value is not None]
        if last:
            labels.append((last[-1][0], to_score(last[-1][1]), name, theme[name]))
    body += _direct_labels(labels)

    body.append(_text(LEFT, DELTA_TOP - 10, "Paired test delta, winner minus seed, with 95% CI", theme["secondary"]))
    body += _y_axis(delta_ticks, to_delta, theme, sign="+")
    body.append(_line(LEFT, to_delta(0.0), WIDTH - RIGHT, to_delta(0.0), theme["axis"], width=1.5))
    for x, entry, delta in zip(xs, entries, deltas):
        if not delta:
            continue
        low, high, point = to_delta(delta["low"]), to_delta(delta["high"]), to_delta(delta["point"])
        tip = (f"{entry['run_id']}: paired test delta {delta['point']:+.3f}, 95% CI [{delta['low']:.3f}, "
               f"{delta['high']:.3f}], {delta['tasks']} tasks")
        body += [f"<g><title>{escape(tip)}</title>",
                 _line(x, low, x, high, theme["winner"], width=2),
                 _line(x - 5, low, x + 5, low, theme["winner"], width=2),
                 _line(x - 5, high, x + 5, high, theme["winner"], width=2),
                 f'<circle cx="{_n(x)}" cy="{_n(point)}" r="4" fill="{theme["winner"]}" '
                 f'stroke="{theme["surface"]}" stroke-width="2"/>', "</g>"]
    if not any(deltas):
        body.append(_text((LEFT + WIDTH - RIGHT) / 2, (DELTA_TOP + DELTA_BOTTOM) / 2 - 6,
                          "No run reached the test split yet", theme["muted"], size=11, anchor="middle"))

    body.append(_line(LEFT, DELTA_BOTTOM + 4, WIDTH - RIGHT, DELTA_BOTTOM + 4, theme["axis"]))
    # One tick per run, so a run that reached no score still shows on the time axis.
    for x, entry in zip(xs, entries):
        tip = f"{entry['run_id']}: {entry.get('outcome') or 'no finish result'}"
        tick = _line(x, DELTA_BOTTOM, x, DELTA_BOTTOM + 8, theme["muted"], width=2)
        body.append(f"<g><title>{escape(tip)}</title>{tick}</g>")
    body += [_text(x, TICK_LABEL_Y, label, theme["muted"], size=11, anchor="middle")
             for x, label in _time_ticks(times, xs)]
    body.append(_text(16, NOTE_Y, NOTE, theme["muted"], size=11))
    return _frame(theme, body)
