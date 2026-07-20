"""
Frequency-based analysis of extracted log templates — Phase 2, Stage 2.

Reads the Drain template output (data/drain_templates.csv) and classifies
each template into a frequency bucket:

    high_volume   — templates occurring ≥ --threshold-high times (default 100)
    medium_volume — templates between --threshold-low and --threshold-high
    low_volume    — templates occurring < --threshold-low times (default 10)

Outputs
-------
data/frequency_report.csv
    One row per template, columns:
        DrainId, EventTemplate, Occurrences, OccurrencePct,
        Category, Recommended_Action

Usage
-----
    python3 frequency_analyzer.py                          # defaults
    python3 frequency_analyzer.py --threshold-high 200    # adjust buckets
    python3 frequency_analyzer.py --input data/drain_templates.csv
    python3 frequency_analyzer.py --fallback               # use loghub EventId/EventTemplate if Drain CSV missing
"""

import argparse
import csv
import os
import sys

DRAIN_TEMPLATES_PATH = "data/drain_templates.csv"
STRUCTURED_LOG_PATH  = "data/Apache_2k.log_structured.csv"
FREQUENCY_REPORT_PATH = "data/frequency_report.csv"

DEFAULT_THRESHOLD_HIGH = 100
DEFAULT_THRESHOLD_LOW  = 10


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_drain_templates(path):
    """Load the CSV produced by drain_parser.py."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows


def load_from_structured_csv(path):
    """
    Fallback: build frequency table directly from the loghub EventId /
    EventTemplate columns when drain_templates.csv has not been created yet.
    """
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    counts = {}      # eventid → count
    templates = {}   # eventid → template string
    reps = {}        # eventid → representative Content

    for row in rows:
        eid = row.get("EventId", "UNKNOWN")
        tmpl = row.get("EventTemplate", row.get("Content", ""))
        counts[eid] = counts.get(eid, 0) + 1
        templates[eid] = tmpl
        if eid not in reps:
            reps[eid] = row.get("Content", "")

    total = sum(counts.values())
    records = []
    for i, (eid, count) in enumerate(
        sorted(counts.items(), key=lambda x: x[1], reverse=True), start=1
    ):
        records.append(
            {
                "DrainId":          eid,
                "EventTemplate":    templates[eid],
                "Occurrences":      count,
                "OccurrencePct":    round(count / total * 100, 2) if total else 0,
                "Representative_Log": reps[eid],
            }
        )
    return records


# ---------------------------------------------------------------------------
# Categorisation
# ---------------------------------------------------------------------------

RECOMMENDED_ACTIONS = {
    "high_volume":   "Bypass LLM — high repetition, classify by rule or template label",
    "medium_volume": "Route to LLM — moderate frequency, semantic review needed",
    "low_volume":    "Route to LLM (priority) — rare events often indicate anomalies",
}


def categorise(records, threshold_high, threshold_low):
    """Add Category and Recommended_Action fields to each record."""
    for r in records:
        count = int(r["Occurrences"])
        if count >= threshold_high:
            cat = "high_volume"
        elif count >= threshold_low:
            cat = "medium_volume"
        else:
            cat = "low_volume"
        r["Category"] = cat
        r["Recommended_Action"] = RECOMMENDED_ACTIONS[cat]
    return records


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

CATEGORY_LABEL = {
    "high_volume":   "HIGH   ",
    "medium_volume": "MEDIUM ",
    "low_volume":    "LOW    ",
}

CATEGORY_SYMBOL = {
    "high_volume":   "[BYPASS]",  # bypass
    "medium_volume": "[ROUTE] ",  # route
    "low_volume":    "[ROUTE!]",  # route (priority)
}


def print_frequency_table(records, threshold_high, threshold_low):
    total = sum(int(r["Occurrences"]) for r in records)
    bypass_count = sum(int(r["Occurrences"]) for r in records if r["Category"] == "high_volume")
    route_count  = total - bypass_count

    print("\n" + "=" * 90)
    print("  FREQUENCY ANALYSIS REPORT")
    print("=" * 90)
    print(
        f"  Thresholds:  high_volume >= {threshold_high}   "
        f"low_volume < {threshold_low}   medium_volume = in between"
    )
    print(f"  Total log lines:  {total}")
    print()
    print(
        f"  {'ID':<8} {'Category':<10} {'Occurrences':>12}  {'%':>6}  Template"
    )
    print("  " + "-" * 84)

    for r in records:
        sym  = CATEGORY_SYMBOL[r["Category"]]
        cat  = CATEGORY_LABEL[r["Category"]]
        tmpl = r["EventTemplate"]
        if len(tmpl) > 48:
            tmpl = tmpl[:45] + "..."
        print(
            f"  {r['DrainId']:<8} {cat:<10} {int(r['Occurrences']):>12}  "
            f"{float(r['OccurrencePct']):>5.1f}%  {sym} {tmpl}"
        )

    print()
    print("  ROUTING PREVIEW (before LLM call):")
    print(f"    [BYPASS] Bypass LLM  (high_volume):  {bypass_count:>6} lines  "
          f"({bypass_count/total*100:.1f}%)  -> no API call needed")
    print(f"    [ROUTE]  Route to LLM:                {route_count:>6} lines  "
          f"({route_count/total*100:.1f}%)  -> LLM classification")
    print(f"\n    Estimated API call reduction:  {bypass_count/total*100:.1f}%")
    print("=" * 90)


def write_frequency_csv(records, output_path):
    fieldnames = [
        "DrainId", "EventTemplate", "Occurrences", "OccurrencePct",
        "Category", "Recommended_Action",
    ]
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in records:
            w.writerow({k: r[k] for k in fieldnames})
    print(f"\nWrote frequency report -> {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyse log template frequencies and categorise for LLM routing."
    )
    parser.add_argument(
        "--input",
        default=DRAIN_TEMPLATES_PATH,
        help="Path to drain_templates.csv produced by drain_parser.py.",
    )
    parser.add_argument(
        "--output",
        default=FREQUENCY_REPORT_PATH,
        help="Output path for the frequency report CSV.",
    )
    parser.add_argument(
        "--threshold-high",
        type=int,
        default=DEFAULT_THRESHOLD_HIGH,
        help="Templates with Occurrences ≥ this are 'high_volume' (default 100).",
    )
    parser.add_argument(
        "--threshold-low",
        type=int,
        default=DEFAULT_THRESHOLD_LOW,
        help="Templates with Occurrences < this are 'low_volume' (default 10).",
    )
    parser.add_argument(
        "--fallback",
        action="store_true",
        help=(
            "If drain_templates.csv does not exist, fall back to reading "
            "EventId/EventTemplate from the structured log CSV."
        ),
    )
    args = parser.parse_args()

    # Load template data
    if os.path.exists(args.input):
        print(f"Loading Drain templates from {args.input} …")
        records = load_drain_templates(args.input)
    elif args.fallback:
        print(
            f"[fallback] {args.input} not found — reading from {STRUCTURED_LOG_PATH} …"
        )
        if not os.path.exists(STRUCTURED_LOG_PATH):
            print(f"ERROR: {STRUCTURED_LOG_PATH} not found either.", file=sys.stderr)
            sys.exit(1)
        records = load_from_structured_csv(STRUCTURED_LOG_PATH)
    else:
        print(
            f"ERROR: {args.input} not found.\n"
            "Run drain_parser.py first, or pass --fallback to use the loghub CSV directly.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not records:
        print("ERROR: No template records found.", file=sys.stderr)
        sys.exit(1)

    # Convert numeric fields
    for r in records:
        r["Occurrences"]   = int(r["Occurrences"])
        r["OccurrencePct"] = float(r["OccurrencePct"])

    records = categorise(records, args.threshold_high, args.threshold_low)
    print_frequency_table(records, args.threshold_high, args.threshold_low)
    write_frequency_csv(records, args.output)

    print(
        f"\nDone.  {len(records)} templates categorised.\n"
        "Next step:  python3 routing.py"
    )


if __name__ == "__main__":
    main()
