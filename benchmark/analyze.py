#!/usr/bin/env python3
"""
Benchmark analysis and report generation.

Reads JSONL results from run.py and generates a markdown report
with context efficiency metrics and comparisons.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median, mean, stdev


# Anthropic Claude pricing (per million tokens)
PRICING = {
    "cache_creation": 3.75,  # $3.75 per MTok
    "cache_read": 0.30,      # $0.30 per MTok
    "output": 15.00,         # $15.00 per MTok
    "input": 3.00,           # $3.00 per MTok
}


def compute_cost_breakdown(run: dict) -> dict[str, float]:
    """Compute cost breakdown by token category."""
    return {
        "cache_creation_cost": run.get("cache_creation_tokens", 0) * PRICING["cache_creation"] / 1_000_000,
        "cache_read_cost": run.get("cache_read_tokens", 0) * PRICING["cache_read"] / 1_000_000,
        "output_cost": run.get("output_tokens", 0) * PRICING["output"] / 1_000_000,
        "input_cost": run.get("input_tokens", 0) * PRICING["input"] / 1_000_000,
    }


def format_cost_breakdown(costs: dict[str, float], indent: str = "  ") -> str:
    """Format cost breakdown as single line."""
    parts = [
        f"cache_create=${costs['cache_creation_cost']:.3f}",
        f"cache_read=${costs['cache_read_cost']:.3f}",
        f"output=${costs['output_cost']:.3f}",
        f"input=${costs['input_cost']:.3f}",
    ]
    return f"{indent}{' '.join(parts)}"


def format_cost_delta(baseline_costs: dict[str, float], tilth_costs: dict[str, float], indent: str = "  ") -> str:
    """Format cost delta breakdown."""
    deltas = {
        "cache_creation": tilth_costs['cache_creation_cost'] - baseline_costs['cache_creation_cost'],
        "cache_read": tilth_costs['cache_read_cost'] - baseline_costs['cache_read_cost'],
        "output": tilth_costs['output_cost'] - baseline_costs['output_cost'],
        "input": tilth_costs['input_cost'] - baseline_costs['input_cost'],
    }
    parts = [
        f"Δcache_create={'+' if deltas['cache_creation'] >= 0 else ''}${deltas['cache_creation']:.3f}",
        f"Δcache_read={'+' if deltas['cache_read'] >= 0 else ''}${deltas['cache_read']:.3f}",
        f"Δoutput={'+' if deltas['output'] >= 0 else ''}${deltas['output']:.3f}",
        f"Δinput={'+' if deltas['input'] >= 0 else ''}${deltas['input']:.3f}",
    ]
    return f"{indent}{' '.join(parts)}"


def load_results(path: Path) -> list[dict]:
    """Load JSONL results file."""
    results = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                results.append(json.loads(line))
    return results


def group_by(results: list[dict], *keys: str) -> dict:
    """Group results by specified keys."""
    groups = defaultdict(list)
    for result in results:
        # Skip error entries that don't have all required fields
        if "error" in result:
            continue
        key = tuple(result.get(k) for k in keys)
        groups[key].append(result)
    return dict(groups)


def compute_stats(values: list) -> dict:
    """Compute statistics for a list of values."""
    if not values:
        return {
            "median": 0,
            "mean": 0,
            "stdev": 0,
            "min": 0,
            "max": 0,
        }

    return {
        "median": median(values),
        "mean": mean(values),
        "stdev": stdev(values) if len(values) > 1 else 0,
        "min": min(values),
        "max": max(values),
    }


def ascii_sparkline(values: list[int]) -> str:
    """Generate ASCII sparkline from values."""
    if not values:
        return ""

    if max(values) == min(values):
        return "▄" * len(values)

    chars = " ▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    return "".join(
        chars[min(int((v - lo) / (hi - lo) * 8), 8)]
        for v in values
    )


def format_delta(baseline_val: float, tilth_val: float) -> str:
    """Format delta as percentage change."""
    if baseline_val == 0:
        return "—"
    pct_change = ((tilth_val - baseline_val) / baseline_val) * 100
    sign = "+" if pct_change > 0 else ""
    return f"{sign}{pct_change:.0f}%"


MODE_ORDER = ["baseline", "tilth", "tilth_forced"]
MODE_LABELS = {
    "baseline": "baseline",
    "tilth": "tilth-added",
    "tilth_forced": "tilth-only",
}


def ordered_modes(mode_names: set[str]) -> list[str]:
    """Return benchmark modes in report order, with unknown modes last."""
    known = [mode for mode in MODE_ORDER if mode in mode_names]
    unknown = sorted(mode_names - set(MODE_ORDER))
    return known + unknown


def mode_label(mode_name: str) -> str:
    return MODE_LABELS.get(mode_name, mode_name)


def format_metric_value(key: str, value: float) -> str:
    if key == "total_cost_usd":
        return f"${value:.4f}"
    return f"{value:.0f}"


def correctness_pct(runs: list[dict]) -> float:
    if not runs:
        return 0.0
    return (sum(1 for r in runs if r["correct"]) / len(runs)) * 100


def find_median_run(runs: list[dict], metric: str) -> dict:
    """Find the run with median value for given metric."""
    if not runs:
        return {}
    sorted_runs = sorted(runs, key=lambda r: r.get(metric, 0))
    return sorted_runs[len(sorted_runs) // 2]


def merge_tool_calls(runs: list[dict]) -> dict[str, float]:
    """Merge tool_calls dicts from multiple runs and compute median counts."""
    # Collect all tool names
    all_tools = set()
    for run in runs:
        if "tool_calls" in run:
            all_tools.update(run["tool_calls"].keys())

    # Compute median count for each tool
    result = {}
    for tool in all_tools:
        counts = [run.get("tool_calls", {}).get(tool, 0) for run in runs]
        result[tool] = median(counts)

    return result


def generate_report(results: list[dict]) -> str:
    """Generate markdown report from results."""
    if not results:
        return "# Error\n\nNo valid results found in file.\n"

    # Filter out error entries
    valid_results = [r for r in results if "error" not in r]
    error_count = len(results) - len(valid_results)

    if not valid_results:
        return f"# Error\n\nAll {len(results)} runs failed.\n"

    # Extract metadata
    models = sorted(set(r["model"] for r in valid_results))
    tasks = sorted(set(r["task"] for r in valid_results))
    modes = ordered_modes(set(r["mode"] for r in valid_results))
    repos = sorted(set(r.get("repo", "synthetic") for r in valid_results))
    max_rep = max(r["repetition"] for r in valid_results)
    num_reps = max_rep + 1

    # Build header
    lines = [
        "# tilth Benchmark Results",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"**Runs:** {len(valid_results)} valid",
    ]

    if error_count > 0:
        lines.append(f" ({error_count} errors)")

    lines.extend([
        f" | **Models:** {', '.join(models)} | **Repos:** {', '.join(repos)} | **Reps:** {num_reps}",
        "",
        "## Context Efficiency",
        "",
        "The primary metric. Context tokens (input + cached) represent the actual context processed each turn. This compounds because each turn re-sends conversation history.",
        "",
        "### Per-task comparison",
        "",
    ])

    # Group by task
    task_groups = group_by(valid_results, "task")

    for task_name in tasks:
        task_results = task_groups.get((task_name,), [])
        if not task_results:
            continue

        lines.append(f"#### {task_name}")
        lines.append("")

        # Show repo for the task
        task_repo = task_results[0].get("repo", "synthetic") if task_results else "synthetic"
        if task_repo != "synthetic":
            lines.append(f"*Repo: {task_repo}*")
            lines.append("")

        # Group by mode and show every present mode side by side.
        mode_groups = group_by(task_results, "mode")
        present_modes = [mode for mode in modes if (mode,) in mode_groups]
        runs_by_mode = {mode: mode_groups[(mode,)] for mode in present_modes}
        has_baseline = "baseline" in runs_by_mode

        if not present_modes:
            lines.append("_No valid mode results._")
            lines.append("")
            continue

        metrics = [
            ("Context tokens", "context_tokens"),
            ("Output tokens", "output_tokens"),
            ("Turns", "num_turns"),
            ("Tool calls", "num_tool_calls"),
            ("Cost USD", "total_cost_usd"),
            ("Duration ms", "duration_ms"),
        ]

        delta_modes = [mode for mode in present_modes if mode != "baseline"] if has_baseline else []
        headers = ["Metric"] + [mode_label(mode) for mode in present_modes]
        headers += [f"{mode_label(mode)} Δ" for mode in delta_modes]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join(["---"] * len(headers)) + "|")

        for label, key in metrics:
            medians = {
                mode: compute_stats([r[key] for r in runs])["median"]
                for mode, runs in runs_by_mode.items()
            }
            row = [f"{label} (median)"]
            row.extend(format_metric_value(key, medians[mode]) for mode in present_modes)
            if has_baseline:
                baseline_value = medians["baseline"]
                row.extend(format_delta(baseline_value, medians[mode]) for mode in delta_modes)
            lines.append("| " + " | ".join(row) + " |")

        correctness = {mode: correctness_pct(runs) for mode, runs in runs_by_mode.items()}
        row = ["Correctness"]
        row.extend(f"{correctness[mode]:.0f}%" for mode in present_modes)
        if has_baseline:
            baseline_correctness = correctness["baseline"]
            row.extend(f"{correctness[mode] - baseline_correctness:+.0f}pp" for mode in delta_modes)
        lines.append("| " + " | ".join(row) + " |")
        lines.append("")

        # Cost breakdown
        median_cost_runs = {
            mode: find_median_run(runs, "total_cost_usd")
            for mode, runs in runs_by_mode.items()
        }
        median_costs = {
            mode: compute_cost_breakdown(run)
            for mode, run in median_cost_runs.items()
        }
        label_width = max(len(mode_label(mode)) for mode in present_modes)

        lines.append("**Cost breakdown (median run):**")
        lines.append("")
        for mode in present_modes:
            run = median_cost_runs[mode]
            total = run.get("total_cost_usd", 0.0)
            turns = run.get("num_turns", 0)
            correct_str = "correct" if run.get("correct", False) else "incorrect"
            label = mode_label(mode).ljust(label_width)
            lines.append(f"  {label}: {turns} turns, ${total:.2f}, {correct_str}")
            lines.append(format_cost_breakdown(median_costs[mode]))

        if has_baseline and delta_modes:
            baseline_run = median_cost_runs["baseline"]
            baseline_costs = median_costs["baseline"]
            baseline_total = baseline_run.get("total_cost_usd", 0.0)
            baseline_turns = baseline_run.get("num_turns", 0)
            for mode in delta_modes:
                run = median_cost_runs[mode]
                total_delta = run.get("total_cost_usd", 0.0) - baseline_total
                turns_delta = run.get("num_turns", 0) - baseline_turns
                lines.append(
                    f"  {mode_label(mode)} vs baseline: "
                    f"{'+' if turns_delta >= 0 else ''}{turns_delta} turns, "
                    f"{'+' if total_delta >= 0 else ''}${total_delta:.2f}"
                )
                lines.append(format_cost_delta(baseline_costs, median_costs[mode]))
        lines.append("")

        # Per-turn sparklines
        median_context_runs = {
            mode: find_median_run(runs, "context_tokens")
            for mode, runs in runs_by_mode.items()
        }
        per_turn_by_mode = {
            mode: run.get("per_turn_context_tokens", [])
            for mode, run in median_context_runs.items()
        }
        if any(per_turn_by_mode.values()):
            lines.append("**Per-turn context tokens (median run):**")
            lines.append("")
            for mode in present_modes:
                per_turn = per_turn_by_mode[mode]
                if not per_turn:
                    continue
                spark = ascii_sparkline(per_turn)
                token_range = f"{min(per_turn):,} → {max(per_turn):,}"
                label = mode_label(mode).ljust(label_width)
                lines.append(f"  {label}: {spark} ({token_range})")
            lines.append("")

        # Tool breakdown
        tools_by_mode = {
            mode: merge_tool_calls(runs)
            for mode, runs in runs_by_mode.items()
        }
        if any(tools_by_mode.values()):
            lines.append("**Tool breakdown (median counts):**")
            lines.append("")
            for mode in present_modes:
                tools = tools_by_mode[mode]
                if not tools:
                    continue
                tool_strs = [f"{name}={count:.0f}" for name, count in sorted(tools.items())]
                label = mode_label(mode).ljust(label_width)
                lines.append(f"  {label}: {', '.join(tool_strs)}")
            lines.append("")

        lines.append("")

    # Summary section (if multiple modes are present)
    runs_by_mode_all = {
        mode: [r for r in valid_results if r["mode"] == mode]
        for mode in modes
    }
    present_modes_all = [mode for mode, runs in runs_by_mode_all.items() if runs]

    if len(present_modes_all) > 1:
        lines.append("## Summary")
        lines.append("")
        lines.append("Averaged across all tasks (median of medians):")
        lines.append("")

        has_baseline = "baseline" in runs_by_mode_all and bool(runs_by_mode_all["baseline"])
        delta_modes = [mode for mode in present_modes_all if mode != "baseline"] if has_baseline else []
        headers = ["Metric"] + [mode_label(mode) for mode in present_modes_all]
        headers += [f"{mode_label(mode)} Δ" for mode in delta_modes]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join(["---"] * len(headers)) + "|")

        metrics = [
            ("Context tokens", "context_tokens"),
            ("Turns", "num_turns"),
            ("Tool calls", "num_tool_calls"),
            ("Cost USD", "total_cost_usd"),
        ]

        for label, key in metrics:
            medians_by_mode = {}
            for mode in present_modes_all:
                by_task = group_by(runs_by_mode_all[mode], "task")
                task_medians = [
                    compute_stats([r[key] for r in runs])["median"]
                    for runs in by_task.values()
                ]
                if task_medians:
                    medians_by_mode[mode] = median(task_medians)

            if not medians_by_mode:
                continue

            row = [label]
            row.extend(
                format_metric_value(key, medians_by_mode.get(mode, 0))
                for mode in present_modes_all
            )
            if has_baseline and "baseline" in medians_by_mode:
                baseline_value = medians_by_mode["baseline"]
                row.extend(
                    format_delta(baseline_value, medians_by_mode[mode])
                    if mode in medians_by_mode else "—"
                    for mode in delta_modes
                )
            lines.append("| " + " | ".join(row) + " |")

        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze benchmark results and generate report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python analyze.py results/benchmark_20260212_150000.jsonl
  python analyze.py results/benchmark_20260212_150000.jsonl -o report.md
        """,
    )

    parser.add_argument(
        "results_file",
        type=Path,
        help="Path to JSONL results file from run.py",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        help="Output path for markdown report (default: print to stdout)",
    )

    args = parser.parse_args()

    # Validate input file
    if not args.results_file.exists():
        print(f"ERROR: File not found: {args.results_file}", file=sys.stderr)
        sys.exit(1)

    # Load and analyze
    try:
        results = load_results(args.results_file)
    except Exception as e:
        print(f"ERROR: Failed to load results: {e}", file=sys.stderr)
        sys.exit(1)

    report = generate_report(results)

    # Output
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report)
        print(f"Report written to: {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
