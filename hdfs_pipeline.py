"""
HDFS Pipeline — runs the full Phase 2 efficiency layer on the HDFS_2k dataset.

Ground truth mapping (from the Level column):
    INFO  -> noise       (routine HDFS operational messages)
    WARN  -> actionable  (warnings that warrant operator attention)

Produces (all in data/):
    hdfs_ground_truth.csv         — 2000 lines with label column
    hdfs_drain_templates.csv      — Drain-extracted templates
    hdfs_frequency_report.csv     — per-template frequency + category
    hdfs_routing_stats.json       — routing summary (for dashboard)
    hdfs_bypassed_logs.csv        — high-volume logs bypassed from LLM
    hdfs_routed_for_llm.csv       — logs forwarded to LLM
    hdfs_predictions.csv          — mock classification results
    hdfs_evaluation_report.json   — accuracy / precision / recall / F1

Usage:
    python3 hdfs_pipeline.py                    # full run (mock classifier)
    python3 hdfs_pipeline.py --threshold 50     # custom high-volume cutoff
    python3 hdfs_pipeline.py --real-llm         # use Gemini (needs API key)
"""

import argparse
import csv
import json
import os
import random
import sys
import time
from collections import Counter

# ── paths ────────────────────────────────────────────────────────────────────
HDFS_RAW_PATH             = "data/HDFS_2k.log_structured.csv"
HDFS_GROUND_TRUTH         = "data/hdfs_ground_truth.csv"
HDFS_DRAIN_TEMPLATES      = "data/hdfs_drain_templates.csv"
HDFS_FREQUENCY_REPORT     = "data/hdfs_frequency_report.csv"
HDFS_ROUTING_STATS        = "data/hdfs_routing_stats.json"
HDFS_BYPASSED_LOGS        = "data/hdfs_bypassed_logs.csv"
HDFS_ROUTED_FOR_LLM       = "data/hdfs_routed_for_llm.csv"
HDFS_PREDICTIONS          = "data/hdfs_predictions.csv"
HDFS_EVAL_REPORT          = "data/hdfs_evaluation_report.json"

DEFAULT_THRESHOLD_HIGH    = 50
DEFAULT_THRESHOLD_LOW     = 10


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 0 — Build HDFS ground truth from Level column
# ═══════════════════════════════════════════════════════════════════════════════

def build_ground_truth(raw_path, output_path):
    """Map Level -> label: INFO=noise, WARN=actionable."""
    with open(raw_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    level_map = {"INFO": "noise", "WARN": "actionable"}
    labeled = []
    unknown = Counter()
    for row in rows:
        level = row.get("Level", "").strip().upper()
        label = level_map.get(level)
        if label is None:
            unknown[level] += 1
            label = "noise"   # safe default
        labeled.append({
            "LineId":        row["LineId"],
            "Time":          f"{row.get('Date','')} {row.get('Time','')}".strip(),
            "Content":       row["Content"],
            "EventId":       row["EventId"],
            "EventTemplate": row["EventTemplate"],
            "Level":         row.get("Level", ""),
            "label":         label,
        })

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["LineId", "Time", "Content", "EventId", "EventTemplate", "Level", "label"])
        w.writeheader()
        w.writerows(labeled)

    n_noise      = sum(1 for r in labeled if r["label"] == "noise")
    n_actionable = sum(1 for r in labeled if r["label"] == "actionable")
    print(f"\n[Stage 0] Ground truth built from Level column:")
    print(f"  INFO  -> noise:      {n_noise}")
    print(f"  WARN  -> actionable: {n_actionable}")
    if unknown:
        print(f"  Unknown levels (defaulted to noise): {dict(unknown)}")
    print(f"  Written -> {output_path}")
    return labeled


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 1 — Drain template extraction
# ═══════════════════════════════════════════════════════════════════════════════

def run_drain(raw_path, output_path):
    """Run Drain3 on HDFS Content column."""
    try:
        from drain3 import TemplateMiner
        from drain3.template_miner_config import TemplateMinerConfig
    except ImportError:
        print("ERROR: drain3 not installed. Run: python3 -m pip install drain3", file=sys.stderr)
        sys.exit(1)

    config = TemplateMinerConfig()
    config.drain_depth    = 4
    config.drain_sim_th   = 0.4
    config.save_state     = False
    miner = TemplateMiner(config=config)

    with open(raw_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print(f"\n[Stage 1] Parsing {len(rows)} HDFS logs with Drain ...")
    for row in rows:
        miner.add_log_message(row["Content"])

    # Collect per-cluster stats
    counts  = {}
    tmpl    = {}
    rep     = {}
    for row in rows:
        result = miner.match(row["Content"])
        if result is None:
            continue
        cid = result.cluster_id
        counts[cid] = counts.get(cid, 0) + 1
        tmpl[cid]   = result.get_template()
        if cid not in rep:
            rep[cid] = row["Content"]

    total = sum(counts.values())
    records = []
    for i, (cid, cnt) in enumerate(
        sorted(counts.items(), key=lambda x: x[1], reverse=True), start=1
    ):
        records.append({
            "DrainId":           f"D{i}",
            "EventTemplate":     tmpl[cid],
            "Occurrences":       cnt,
            "OccurrencePct":     round(cnt / total * 100, 2) if total else 0,
            "Representative_Log": rep[cid],
        })

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["DrainId", "EventTemplate", "Occurrences", "OccurrencePct", "Representative_Log"])
        w.writeheader()
        w.writerows(records)

    print(f"  {len(records)} templates discovered")
    for r in records:
        print(f"    {r['DrainId']:<6} {r['Occurrences']:>5}  {r['EventTemplate'][:60]}")
    print(f"  Written -> {output_path}")
    return records


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 2 — Frequency analysis
# ═══════════════════════════════════════════════════════════════════════════════

def run_frequency(drain_records, threshold_high, threshold_low, output_path):
    """Categorise templates into high/medium/low volume."""
    total = sum(int(r["Occurrences"]) for r in drain_records)

    result = []
    for r in drain_records:
        cnt = int(r["Occurrences"])
        if cnt >= threshold_high:
            cat = "high_volume"
            action = "Bypass LLM"
        elif cnt >= threshold_low:
            cat = "medium_volume"
            action = "Route to LLM"
        else:
            cat = "low_volume"
            action = "Route to LLM (priority)"
        result.append({**r, "Category": cat, "Recommended_Action": action})

    bypass = sum(int(r["Occurrences"]) for r in result if r["Category"] == "high_volume")
    route  = total - bypass

    print(f"\n[Stage 2] Frequency analysis (threshold >= {threshold_high} = high_volume):")
    print(f"  {'DrainId':<8} {'Count':>6}  {'%':>5}  Category       Template")
    print("  " + "-" * 75)
    for r in result:
        cat_label = {"high_volume": "HIGH  ", "medium_volume": "MEDIUM", "low_volume": "LOW   "}[r["Category"]]
        t = r["EventTemplate"][:45] + "..." if len(r["EventTemplate"]) > 45 else r["EventTemplate"]
        print(f"  {r['DrainId']:<8} {int(r['Occurrences']):>6}  {float(r['OccurrencePct']):>4.1f}%  {cat_label}  {t}")
    print(f"\n  [BYPASS] Bypassed (no LLM): {bypass} ({bypass/total*100:.1f}%)")
    print(f"  [ROUTE]  Forwarded to LLM:  {route} ({route/total*100:.1f}%)")
    print(f"  API savings: {bypass/total*100:.1f}%")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        fieldnames = ["DrainId", "EventTemplate", "Occurrences", "OccurrencePct", "Category", "Recommended_Action"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in result:
            w.writerow({k: r[k] for k in fieldnames})
    print(f"  Written -> {output_path}")
    return result, bypass, route, total


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 3 — Frequency-aware routing
# ═══════════════════════════════════════════════════════════════════════════════

def run_routing(
    ground_truth_rows, freq_records, threshold,
    routing_stats_path, routed_path, bypassed_path
):
    """Partition logs into bypass / route streams."""
    # Build category map from Drain IDs  — but ground truth uses loghub EventIds.
    # We match by template occurrence counts directly.
    # Strategy: for each EventId in ground_truth, compute total occurrences,
    # then classify by threshold.
    event_counts = Counter(r["EventId"] for r in ground_truth_rows)
    total = len(ground_truth_rows)

    # Also build a label map from freq_records for bypass propagation
    # (freq_records use DrainId, not EventId — we use count threshold directly)
    bypass_rows = []
    route_rows  = []

    for row in ground_truth_rows:
        cnt = event_counts[row["EventId"]]
        if cnt >= threshold:
            # Bypass: use the ground-truth label directly (from Level column)
            bypass_rows.append({**row, "auto_label": row["label"], "routing_reason": "high_volume_bypass"})
        else:
            route_rows.append(row)

    bypass_label_breakdown = dict(Counter(r["auto_label"] for r in bypass_rows))
    bypassed_pct = len(bypass_rows) / total * 100 if total else 0
    routed_pct   = len(route_rows) / total * 100 if total else 0

    stats = {
        "dataset":              "HDFS_2k",
        "threshold":            threshold,
        "total_logs":           total,
        "bypassed":             len(bypass_rows),
        "bypassed_pct":         round(bypassed_pct, 2),
        "routed_to_llm":        len(route_rows),
        "routed_pct":           round(routed_pct, 2),
        "api_call_savings_pct": round(bypassed_pct, 2),
        "bypass_label_breakdown": bypass_label_breakdown,
        "routed_for_llm_path":  routed_path,
        "bypassed_logs_path":   bypassed_path,
    }

    print(f"\n[Stage 3] Routing (threshold >= {threshold}):")
    print(f"  Total:    {total}")
    print(f"  Bypassed: {len(bypass_rows)} ({bypassed_pct:.1f}%)")
    print(f"  Routed:   {len(route_rows)} ({routed_pct:.1f}%)")
    print(f"  Savings:  {bypassed_pct:.1f}%")

    with open(routing_stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)

    gt_fields = list(ground_truth_rows[0].keys())
    with open(routed_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=gt_fields)
        w.writeheader()
        w.writerows(route_rows)

    bypass_fields = gt_fields + ["auto_label", "routing_reason"]
    with open(bypassed_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=bypass_fields)
        w.writeheader()
        w.writerows(bypass_rows)

    print(f"  Written -> {routing_stats_path}")
    print(f"  Written -> {routed_path}")
    print(f"  Written -> {bypassed_path}")
    return bypass_rows, route_rows, stats


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 4 — Classification (mock or real LLM)
# ═══════════════════════════════════════════════════════════════════════════════

HDFS_SYSTEM_PROMPT = """You are an SRE assistant classifying HDFS (Hadoop Distributed File System) log lines.
Classify each line as one of exactly two labels:
- "noise": routine, informational, or expected HDFS operation. No operator action needed.
- "actionable": indicates a real problem or warning worth an operator's attention.

Examples:
Log: "PacketResponder 1 for block blk_38865049064139660 terminating"
Label: noise (routine block replication completion — expected behavior)

Log: "BLOCK* NameSystem.addStoredBlock: blockMap updated: 10.251.73.220:50010 is added to blk_7128 size 67108864"
Label: noise (routine blockMap update — normal HDFS operation)

Log: "Exception in receiveBlock for block blk_-1791657668697213903"
Label: actionable (explicit exception on block receive — needs investigation)

Log: "Received exception while serving blk_-1791657668697213903 to /10.251.42.84"
Label: actionable (exception serving a block — potential data loss risk)

Now classify the following NEW log lines.
Respond with ONLY a JSON array of objects like [{"id": 1, "label": "noise"}, ...],
one object per numbered line below, no other text, no markdown fences.

Lines to classify:
"""


def mock_classify(lines):
    """Heuristic classifier for HDFS: WARN-level content patterns -> actionable."""
    actionable_keywords = [
        "exception", "error", "failed", "failure", "warn", "corrupt",
        "lost", "missing", "timeout", "refused", "unreachable", "invalid",
    ]
    labels = []
    for line in lines:
        lower = line.lower()
        if any(kw in lower for kw in actionable_keywords):
            labels.append("actionable")
        else:
            labels.append("noise")
    return labels


def call_gemini_batch(batch_lines, model_name="gemini-3.5-flash", max_retries=5):
    import google.generativeai as genai
    from google.api_core.exceptions import ResourceExhausted

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: set GEMINI_API_KEY environment variable.", file=sys.stderr)
        sys.exit(1)
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)

    numbered = "\n".join(f'{i+1}. "{line}"' for i, line in enumerate(batch_lines))
    prompt = HDFS_SYSTEM_PROMPT + numbered

    wait = 13
    for attempt in range(max_retries):
        try:
            response = model.generate_content(prompt)
            text = response.text.strip()
            if text.startswith("```"):
                text = text.strip("`")
                text = text[text.find("["):]
            parsed = json.loads(text)
            labels = [None] * len(batch_lines)
            for item in parsed:
                idx = item["id"] - 1
                if 0 <= idx < len(labels):
                    labels[idx] = item["label"]
            return labels
        except ResourceExhausted:
            if attempt == max_retries - 1:
                raise
            print(f"  rate limited, waiting {wait:.0f}s ...")
            time.sleep(wait)
            wait = min(wait * 1.5, 60)


def run_classification(routed_rows, use_real_llm, output_path, sample_n=200, batch_size=20, seed=42):
    """Classify routed logs and write predictions CSV."""
    random.Random(seed).shuffle(routed_rows)
    sample = routed_rows[:sample_n]

    mode = "Gemini API" if use_real_llm else "MOCK heuristic"
    print(f"\n[Stage 4] Classifying {len(sample)} routed logs via {mode} ...")

    all_preds = []
    for i in range(0, len(sample), batch_size):
        batch = sample[i:i + batch_size]
        lines = [r["Content"] for r in batch]

        if use_real_llm:
            labels = call_gemini_batch(lines)
            time.sleep(13)
        else:
            labels = mock_classify(lines)

        for row, label in zip(batch, labels):
            all_preds.append({
                "LineId":          row["LineId"],
                "Content":         row["Content"],
                "true_label":      row["label"],
                "predicted_label": label or "noise",
                "EventId":         row.get("EventId", ""),
                "Level":           row.get("Level", ""),
            })
        print(f"  batch {i // batch_size + 1}/{(len(sample)-1) // batch_size + 1} done")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["LineId", "Content", "true_label", "predicted_label", "EventId", "Level"])
        w.writeheader()
        w.writerows(all_preds)

    print(f"  Written {len(all_preds)} predictions -> {output_path}")
    return all_preds


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 5 — Evaluation
# ═══════════════════════════════════════════════════════════════════════════════

def run_evaluation(predictions, output_path, is_mock):
    """Compute accuracy, precision, recall, F1 and write JSON report."""
    try:
        from sklearn.metrics import accuracy_score, precision_recall_fscore_support, confusion_matrix
    except ImportError:
        print("WARNING: scikit-learn not installed — skipping evaluation.", file=sys.stderr)
        return

    y_true = [p["true_label"] for p in predictions]
    y_pred = [p["predicted_label"] for p in predictions]
    labels = ["noise", "actionable"]

    acc = accuracy_score(y_true, y_pred)
    precision, recall, f1, support = precision_recall_fscore_support(
        y_true, y_pred, labels=labels, zero_division=0
    )
    cm = confusion_matrix(y_true, y_pred, labels=labels)

    print(f"\n[Stage 5] Evaluation results{'  (MOCK)' if is_mock else ''}:")
    print(f"  Accuracy: {acc:.3f}")
    print(f"  {'label':<12} {'precision':<12} {'recall':<12} {'f1':<12} support")
    for i, lbl in enumerate(labels):
        print(f"  {lbl:<12} {precision[i]:<12.3f} {recall[i]:<12.3f} {f1[i]:<12.3f} {support[i]}")

    wrong = [p for p in predictions if p["true_label"] != p["predicted_label"]]
    print(f"  Misclassified: {len(wrong)}/{len(predictions)}")

    report = {
        "dataset":    "HDFS_2k",
        "source_file": output_path,
        "is_mock":    is_mock,
        "n":          len(predictions),
        "accuracy":   float(acc),
        "per_label": {
            lbl: {
                "precision": float(precision[i]),
                "recall":    float(recall[i]),
                "f1":        float(f1[i]),
                "support":   int(support[i]),
            }
            for i, lbl in enumerate(labels)
        },
        "confusion_matrix": {"labels": labels, "matrix": cm.tolist()},
        "misclassified_count": len(wrong),
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"  Written -> {output_path}")
    return report


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Full HDFS pipeline: ground-truth -> Drain -> frequency -> routing -> classify -> evaluate."
    )
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD_HIGH,
                        help=f"High-volume threshold (default: {DEFAULT_THRESHOLD_HIGH})")
    parser.add_argument("--threshold-low", type=int, default=DEFAULT_THRESHOLD_LOW,
                        help=f"Low-volume threshold (default: {DEFAULT_THRESHOLD_LOW})")
    parser.add_argument("--sample", type=int, default=200,
                        help="Number of routed logs to classify (default: 200)")
    parser.add_argument("--real-llm", action="store_true",
                        help="Use Gemini API instead of mock heuristic (needs GEMINI_API_KEY)")
    args = parser.parse_args()

    if not os.path.exists(HDFS_RAW_PATH):
        print(f"ERROR: {HDFS_RAW_PATH} not found.\nCopy HDFS_2k.log_structured.csv into the data/ directory.", file=sys.stderr)
        sys.exit(1)

    print("=" * 65)
    print("  HDFS PIPELINE — Phase 2 + Classification + Evaluation")
    print("=" * 65)

    # Stage 0: Ground truth
    gt_rows = build_ground_truth(HDFS_RAW_PATH, HDFS_GROUND_TRUTH)

    # Stage 1: Drain
    drain_records = run_drain(HDFS_RAW_PATH, HDFS_DRAIN_TEMPLATES)

    # Stage 2: Frequency
    freq_records, bypass_count, route_count, total = run_frequency(
        drain_records, args.threshold, args.threshold_low, HDFS_FREQUENCY_REPORT
    )

    # Stage 3: Routing
    bypass_rows, route_rows, routing_stats = run_routing(
        gt_rows, freq_records, args.threshold,
        HDFS_ROUTING_STATS, HDFS_ROUTED_FOR_LLM, HDFS_BYPASSED_LOGS
    )

    # Stage 4: Classification
    use_real_llm = args.real_llm
    predictions = run_classification(
        route_rows, use_real_llm, HDFS_PREDICTIONS, sample_n=args.sample
    )

    # Stage 5: Evaluation
    run_evaluation(predictions, HDFS_EVAL_REPORT, is_mock=not use_real_llm)

    print("\n" + "=" * 65)
    print("  HDFS PIPELINE COMPLETE")
    print("=" * 65)
    print(f"  Ground truth:      {HDFS_GROUND_TRUTH}")
    print(f"  Drain templates:   {HDFS_DRAIN_TEMPLATES}")
    print(f"  Frequency report:  {HDFS_FREQUENCY_REPORT}")
    print(f"  Routing stats:     {HDFS_ROUTING_STATS}")
    print(f"  Predictions:       {HDFS_PREDICTIONS}")
    print(f"  Evaluation:        {HDFS_EVAL_REPORT}")
    print(f"\n  Open Streamlit:    streamlit run review_predictions.py")


if __name__ == "__main__":
    main()
