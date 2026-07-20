"""
Frequency-aware LLM routing — Phase 2, Stage 3.

Reads the frequency report (data/frequency_report.csv) and the full log
dataset (data/ground_truth.csv or Apache_2k structured CSV) and partitions
log lines into two streams:

  BYPASS  — high_volume templates: skip LLM, propagate the template-level
             label from data/template_ground_truth.csv (or mark heuristically).
  ROUTE   — medium_volume and low_volume templates: forward to LLM for
             semantic classification.

Outputs
-------
data/routed_for_llm.csv
    Log lines selected for LLM classification.  Has the same columns as
    ground_truth.csv so it is a drop-in input for few_shot_classifier.py.

data/bypassed_logs.csv
    Log lines that were handled without LLM invocation, with an auto_label
    column showing the propagated or heuristic label.

data/routing_stats.json
    Machine-readable routing summary (used by the Streamlit dashboard).

Usage
-----
    python3 routing.py                          # defaults
    python3 routing.py --threshold 200          # change high-volume cutoff
    python3 routing.py --sample 200             # limit LLM-bound rows (for testing)
    python3 routing.py --no-template-labels     # skip propagation, mark bypassed as 'noise'

Integration
-----------
Pass the routed CSV to the classifier:
    python3 few_shot_classifier.py --input data/routed_for_llm.csv --sample 200
"""

import argparse
import csv
import json
import os
import sys

FREQUENCY_REPORT_PATH    = "data/frequency_report.csv"
GROUND_TRUTH_PATH        = "data/ground_truth.csv"
STRUCTURED_LOG_PATH      = "data/Apache_2k.log_structured.csv"
TEMPLATE_GT_PATH         = "data/template_ground_truth.csv"

ROUTED_FOR_LLM_PATH      = "data/routed_for_llm.csv"
BYPASSED_LOGS_PATH       = "data/bypassed_logs.csv"
ROUTING_STATS_PATH       = "data/routing_stats.json"

DEFAULT_THRESHOLD        = 100   # occurrences above this → high_volume → bypass


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_frequency_report(path):
    """Return a dict: DrainId/EventId → category ('high_volume' | 'medium_volume' | 'low_volume')."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {r["DrainId"]: r["Category"] for r in rows}, rows


def load_logs(path):
    """Load ground_truth.csv (preferred) or fall back to structured CSV."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows


def load_template_labels(path):
    """Load template-level labels → dict: EventId → proposed_label."""
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {r["EventId"]: r["proposed_label"] for r in rows}


# ---------------------------------------------------------------------------
# Routing logic
# ---------------------------------------------------------------------------

def route(
    log_rows,
    category_by_event,
    template_labels,
    threshold,
    use_template_labels,
    sample_n,
    seed=42,
):
    """
    Partition log_rows into (bypass_rows, route_rows).

    category_by_event maps EventId → category string.  When the frequency
    report was built from Drain IDs (D1, D2, …) rather than loghub EventIds
    (E1, E2, …), we fall back to an occurrence-count heuristic using the
    structured CSV frequency.
    """
    bypass_rows = []
    route_rows  = []

    # Build a frequency count from the logs directly as a fallback
    event_counts = {}
    for row in log_rows:
        eid = row.get("EventId", "UNKNOWN")
        event_counts[eid] = event_counts.get(eid, 0) + 1

    for row in log_rows:
        eid = row.get("EventId", "UNKNOWN")

        # Determine category: prefer frequency_report lookup, else threshold on count
        category = category_by_event.get(eid)
        if category is None:
            count = event_counts.get(eid, 0)
            if count >= threshold:
                category = "high_volume"
            elif count >= 10:
                category = "medium_volume"
            else:
                category = "low_volume"

        if category == "high_volume":
            # Determine bypass label
            if use_template_labels and eid in template_labels:
                auto_label = template_labels[eid]
            else:
                # Heuristic: high-volume templates in Apache logs tend to be
                # routine (worker registration, config loading).  Mark as noise
                # unless the template-level label says otherwise.
                auto_label = "noise"
            bypass_rows.append({**row, "auto_label": auto_label, "routing_reason": "high_volume_bypass"})
        else:
            route_rows.append(row)

    # Optionally limit the LLM-bound set for testing
    if sample_n and len(route_rows) > sample_n:
        import random
        rng = random.Random(seed)
        rng.shuffle(route_rows)
        route_rows = route_rows[:sample_n]

    return bypass_rows, route_rows


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def write_routed_csv(rows, path, is_bypass=False):
    if not rows:
        return
    all_keys = list(rows[0].keys())
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=all_keys)
        w.writeheader()
        w.writerows(rows)


def print_routing_summary(bypass_rows, route_rows, total, threshold):
    bypassed = len(bypass_rows)
    routed   = len(route_rows)
    saving_pct = bypassed / total * 100 if total else 0

    print("\n" + "=" * 70)
    print("  FREQUENCY-AWARE LLM ROUTING  --  SUMMARY")
    print("=" * 70)
    print(f"\n  High-volume threshold:    >= {threshold} occurrences per template")
    print()
    print(f"  Total log lines:          {total:>6}")
    print(f"  [BYPASS] Bypassed (no LLM):    {bypassed:>6}  ({bypassed/total*100:.1f}%)")
    print(f"  [ROUTE]  Forwarded to LLM:     {routed:>6}  ({routed/total*100:.1f}%)")
    print(f"  API call savings:              {saving_pct:.1f}%")
    print()

    # Show per-category detail for bypassed
    reason_counts = {}
    for row in bypass_rows:
        label = row.get("auto_label", "?")
        reason_counts[label] = reason_counts.get(label, 0) + 1
    if reason_counts:
        print("  Bypassed label breakdown:")
        for label, count in reason_counts.items():
            print(f"    {label:<12} {count:>6}")

    print()
    print(f"  Wrote -> {ROUTED_FOR_LLM_PATH}")
    print(f"  Wrote -> {BYPASSED_LOGS_PATH}")
    print(f"  Wrote -> {ROUTING_STATS_PATH}")
    print("=" * 70)
    print(
        f"\n  Next step:\n"
        f"    python3 few_shot_classifier.py --input {ROUTED_FOR_LLM_PATH}"
    )


def write_routing_stats(bypass_rows, route_rows, total, threshold, output_path):
    bypassed    = len(bypass_rows)
    routed      = len(route_rows)
    saving_pct  = bypassed / total * 100 if total else 0

    label_breakdown = {}
    for row in bypass_rows:
        lbl = row.get("auto_label", "unknown")
        label_breakdown[lbl] = label_breakdown.get(lbl, 0) + 1

    stats = {
        "threshold":        threshold,
        "total_logs":       total,
        "bypassed":         bypassed,
        "bypassed_pct":     round(saving_pct, 2),
        "routed_to_llm":    routed,
        "routed_pct":       round(routed / total * 100, 2) if total else 0,
        "api_call_savings_pct": round(saving_pct, 2),
        "bypass_label_breakdown": label_breakdown,
        "routed_for_llm_path": ROUTED_FOR_LLM_PATH,
        "bypassed_logs_path":  BYPASSED_LOGS_PATH,
    }
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Route logs: high-volume bypass LLM, rest forwarded to classifier."
    )
    parser.add_argument(
        "--frequency-report",
        default=FREQUENCY_REPORT_PATH,
        help="Path to frequency_report.csv from frequency_analyzer.py.",
    )
    parser.add_argument(
        "--logs",
        default=None,
        help=(
            "Path to log CSV (ground_truth.csv preferred; falls back to "
            "Apache_2k structured CSV). Default: auto-detect."
        ),
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help=f"Occurrences >= threshold -> high_volume -> bypass LLM. Default: {DEFAULT_THRESHOLD}.",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Maximum number of rows to forward to the LLM (for testing).",
    )
    parser.add_argument(
        "--no-template-labels",
        action="store_true",
        help="Do not use template_ground_truth.csv labels for bypassed logs; mark all as 'noise'.",
    )
    parser.add_argument(
        "--output-routed",
        default=ROUTED_FOR_LLM_PATH,
        help="Output CSV for logs routed to LLM.",
    )
    parser.add_argument(
        "--output-bypassed",
        default=BYPASSED_LOGS_PATH,
        help="Output CSV for bypassed logs.",
    )
    parser.add_argument(
        "--output-stats",
        default=ROUTING_STATS_PATH,
        help="Output JSON for routing statistics.",
    )
    args = parser.parse_args()

    # ---- Load frequency report ----
    if not os.path.exists(args.frequency_report):
        print(
            f"ERROR: {args.frequency_report} not found.\n"
            "Run frequency_analyzer.py first.",
            file=sys.stderr,
        )
        sys.exit(1)
    category_by_event, freq_rows = load_frequency_report(args.frequency_report)
    print(f"Loaded {len(category_by_event)} template categories from {args.frequency_report}.")

    # ---- Load log lines ----
    logs_path = args.logs
    if logs_path is None:
        if os.path.exists(GROUND_TRUTH_PATH):
            logs_path = GROUND_TRUTH_PATH
        elif os.path.exists(STRUCTURED_LOG_PATH):
            logs_path = STRUCTURED_LOG_PATH
        else:
            print(
                f"ERROR: Cannot find ground_truth.csv or Apache_2k structured CSV.\n"
                "Run labeling_interface.py --propagate to create ground_truth.csv.",
                file=sys.stderr,
            )
            sys.exit(1)
    print(f"Loading logs from {logs_path} …")
    log_rows = load_logs(logs_path)
    total    = len(log_rows)
    print(f"  {total} log lines loaded.")

    # ---- Load template-level labels for bypass propagation ----
    template_labels = {} if args.no_template_labels else load_template_labels(TEMPLATE_GT_PATH)
    if template_labels:
        print(f"  {len(template_labels)} template labels loaded from {TEMPLATE_GT_PATH}.")

    # ---- Route ----
    bypass_rows, route_rows = route(
        log_rows,
        category_by_event,
        template_labels,
        threshold=args.threshold,
        use_template_labels=not args.no_template_labels,
        sample_n=args.sample,
    )

    # ---- Write outputs ----
    write_routed_csv(route_rows,  args.output_routed,   is_bypass=False)
    write_routed_csv(bypass_rows, args.output_bypassed, is_bypass=True)
    write_routing_stats(bypass_rows, route_rows, total, args.threshold, args.output_stats)

    # ---- Print summary ----
    print_routing_summary(bypass_rows, route_rows, total, args.threshold)


if __name__ == "__main__":
    main()
