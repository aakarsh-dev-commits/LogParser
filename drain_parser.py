"""
Drain-based log template extraction — Phase 2, Stage 1.

Runs the Drain online log-parsing algorithm on the raw Content column of any
structured log CSV and produces a template summary.  The loghub Apache_2k CSV
already ships pre-parsed EventId/EventTemplate columns; this script re-derives
templates independently from the Content text so the algorithm is actually
exercised rather than just read from a column.

Outputs
-------
data/drain_templates.csv
    One row per discovered template with columns:
        DrainId, EventTemplate, Occurrences, OccurrencePct, Representative_Log

Usage
-----
    python3 drain_parser.py                        # default: Apache_2k structured CSV
    python3 drain_parser.py --input <path.csv>     # any CSV with a 'Content' column
    python3 drain_parser.py --compare              # compare with loghub EventTemplate column

Requires
--------
    pip install drain3
"""

import argparse
import csv
import os
import sys

STRUCTURED_LOG_PATH = "data/Apache_2k.log_structured.csv"
DRAIN_TEMPLATES_PATH = "data/drain_templates.csv"


# ---------------------------------------------------------------------------
# Drain3 wrapper
# ---------------------------------------------------------------------------

def build_drain_miner():
    """Return a configured Drain3 LogCluster miner."""
    try:
        from drain3 import TemplateMiner
        from drain3.template_miner_config import TemplateMinerConfig
    except ImportError:
        print(
            "ERROR: drain3 is not installed.\n"
            "Run:  pip install drain3",
            file=sys.stderr,
        )
        sys.exit(1)

    config = TemplateMinerConfig()
    # Depth-4 parse tree, similarity threshold 0.4 — good defaults for Apache logs
    config.drain_depth = 4
    config.drain_sim_th = 0.4
    # Don't write a persistent state file for this demo
    config.save_state = False
    miner = TemplateMiner(config=config)
    return miner


# ---------------------------------------------------------------------------
# Core parsing logic
# ---------------------------------------------------------------------------

def parse_logs(input_path):
    """
    Feed every Content line through Drain and return:
        (miner, rows)  where rows is the list of CSV dicts read.
    """
    miner = build_drain_miner()

    with open(input_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if "Content" not in rows[0]:
        print(f"ERROR: CSV at '{input_path}' has no 'Content' column.", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing {len(rows)} log lines with Drain …")
    for row in rows:
        miner.add_log_message(row["Content"])

    return miner, rows


def collect_template_stats(miner, rows):
    """
    Match every raw log back to its Drain cluster and collect per-template
    occurrence counts and one representative raw log line.
    """
    from drain3 import TemplateMiner  # already imported, just for isinstance check

    cluster_counts = {}   # cluster_id → count
    cluster_rep    = {}   # cluster_id → representative raw line
    cluster_tmpl   = {}   # cluster_id → template string

    for row in rows:
        result = miner.match(row["Content"])
        if result is None:
            continue
        cid = result.cluster_id
        cluster_counts[cid] = cluster_counts.get(cid, 0) + 1
        cluster_tmpl[cid]   = result.get_template()
        if cid not in cluster_rep:
            cluster_rep[cid] = row["Content"]

    total = sum(cluster_counts.values())
    records = []
    for i, (cid, count) in enumerate(
        sorted(cluster_counts.items(), key=lambda x: x[1], reverse=True), start=1
    ):
        records.append(
            {
                "DrainId":          f"D{i}",
                "EventTemplate":    cluster_tmpl[cid],
                "Occurrences":      count,
                "OccurrencePct":    round(count / total * 100, 2) if total else 0,
                "Representative_Log": cluster_rep[cid],
            }
        )
    return records


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def write_templates_csv(records, output_path):
    fieldnames = ["DrainId", "EventTemplate", "Occurrences", "OccurrencePct", "Representative_Log"]
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(records)
    print(f"\nWrote {len(records)} templates -> {output_path}")


def print_template_table(records):
    print("\n" + "=" * 80)
    print("  DRAIN TEMPLATE EXTRACTION RESULTS")
    print("=" * 80)
    print(f"  {'ID':<6} {'Occurrences':>12}  {'%':>6}  Template")
    print("  " + "-" * 76)
    for r in records:
        tmpl = r["EventTemplate"]
        if len(tmpl) > 52:
            tmpl = tmpl[:49] + "..."
        print(f"  {r['DrainId']:<6} {r['Occurrences']:>12}  {r['OccurrencePct']:>5.1f}%  {tmpl}")
    print("=" * 80)


def compare_with_loghub(records, input_path):
    """
    Side-by-side comparison of Drain-discovered templates vs. loghub's
    pre-parsed EventTemplate column (available in the Apache_2k CSV).
    """
    with open(input_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if "EventTemplate" not in rows[0]:
        print("\n[compare] No 'EventTemplate' column in source CSV — skipping comparison.")
        return

    # Collect loghub templates
    loghub_templates = {}
    for row in rows:
        eid = row.get("EventId", "?")
        tmpl = row["EventTemplate"]
        loghub_templates[eid] = tmpl

    print("\n" + "=" * 80)
    print("  COMPARISON: Drain (this script) vs. loghub pre-parsed templates")
    print("=" * 80)
    print(f"\n  {'Drain ID':<10} {'Drain Template':<45} {'loghub Match?'}")
    print("  " + "-" * 76)

    loghub_set = set(loghub_templates.values())
    matched = 0
    for r in records:
        dt = r["EventTemplate"]
        # Fuzzy-match: strip <*> tokens and check overlap
        dt_words = set(dt.replace("<*>", "").split())
        best_match = None
        best_score = 0
        for lt in loghub_set:
            lt_words = set(lt.replace("<*>", "").split())
            if not dt_words or not lt_words:
                continue
            overlap = len(dt_words & lt_words) / max(len(dt_words), len(lt_words))
            if overlap > best_score:
                best_score = overlap
                best_match = lt
        is_match = "[match]" if best_score >= 0.7 else "[~]  "
        if best_score >= 0.7:
            matched += 1
        disp_dt = dt[:44] if len(dt) <= 44 else dt[:41] + "..."
        print(f"  {r['DrainId']:<10} {disp_dt:<45} {is_match}  ({best_score:.0%} overlap)")

    print(f"\n  {matched}/{len(records)} Drain templates matched a loghub template (>=70% word overlap).")
    print("=" * 80)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Extract log templates using the Drain algorithm."
    )
    parser.add_argument(
        "--input",
        default=STRUCTURED_LOG_PATH,
        help="Path to a structured log CSV with a 'Content' column.",
    )
    parser.add_argument(
        "--output",
        default=DRAIN_TEMPLATES_PATH,
        help="Output path for the templates CSV.",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Compare Drain output with the loghub EventTemplate column (if present).",
    )
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"ERROR: Input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    miner, rows = parse_logs(args.input)
    records = collect_template_stats(miner, rows)

    print_template_table(records)
    write_templates_csv(records, args.output)

    if args.compare:
        compare_with_loghub(records, args.input)

    print(
        f"\nDone.  {len(records)} templates discovered from {len(rows)} log lines."
        f"\nNext step:  python3 frequency_analyzer.py"
    )


if __name__ == "__main__":
    main()
