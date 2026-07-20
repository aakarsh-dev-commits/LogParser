"""Create evidence-grounded incident reports from classified Apache logs.

This is deliberately a second step after ``few_shot_classifier.py``:

    log line -> few-shot classification -> incident grouping -> incident report

It never treats an LLM-generated diagnosis as evidence. Evidence is calculated
locally from the source logs, then supplied to Gemini only to write a concise
operator-facing explanation.

Usage:
    # Uses real classifications made by few_shot_classifier.py
    python3 incident_reporter.py --predictions data/predictions.csv

    # Exercises grouping/report formatting without an API call. Clearly marked
    # as MOCK in the output; do not present it as Gemini output.
    python3 incident_reporter.py --predictions data/predictions.csv --mock
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime


STRUCTURED_LOG_PATH = "data/Apache_2k.log_structured.csv"
DEFAULT_PREDICTIONS_PATH = "data/predictions.csv"
DEFAULT_OUTPUT_PATH = "data/incident_reports.json"


def parse_time(value):
    return datetime.strptime(value, "%a %b %d %H:%M:%S %Y")


def load_logs(path):
    with open(path, newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    return {row["LineId"]: row for row in rows}, rows


def load_actionable_predictions(path, logs_by_id, label_column):
    with open(path, newline="", encoding="utf-8") as file:
        predictions = list(csv.DictReader(file))

    if not predictions:
        return []
    if label_column == "auto":
        label_column = "verified_label" if "verified_label" in predictions[0] else "predicted_label"
    if label_column not in predictions[0]:
        raise ValueError(f"Column '{label_column}' is not in {path}. Available: {list(predictions[0])}")

    actionable = []
    for prediction in predictions:
        if prediction.get(label_column, "").strip().lower() != "actionable":
            continue
        log = logs_by_id.get(prediction["LineId"])
        if log:
            actionable.append(log)
    return actionable


def nearby_lines(all_logs, centre_time, seconds=60, limit=6):
    """Return neighbouring lines in a time window, excluding routine notices."""
    candidates = []
    for row in all_logs:
        delta = abs((parse_time(row["Time"]) - centre_time).total_seconds())
        if 0 < delta <= seconds and row["Level"].lower() == "error":
            candidates.append((delta, row["Content"]))
    return [content for _, content in sorted(candidates)[:limit]]


def build_incident_facts(actionable_logs, all_logs):
    """Group same-template findings into incident candidates with factual evidence."""
    by_template = defaultdict(list)
    for row in actionable_logs:
        by_template[row["EventTemplate"]].append(row)

    incidents = []
    for template, rows in by_template.items():
        rows.sort(key=lambda row: parse_time(row["Time"]))
        times = [parse_time(row["Time"]) for row in rows]
        raw_examples = list(dict.fromkeys(row["Content"] for row in rows))[:3]
        neighbouring = nearby_lines(all_logs, times[0])
        incidents.append({
            "incident_key": rows[0]["EventId"],
            "event_template": template,
            "occurrences_in_classified_sample": len(rows),
            "first_seen": rows[0]["Time"],
            "last_seen": rows[-1]["Time"],
            "representative_logs": raw_examples,
            "nearby_error_logs": list(dict.fromkeys(neighbouring))[:3],
            "level_distribution": dict(Counter(row["Level"].lower() for row in rows)),
        })
    return sorted(incidents, key=lambda item: item["occurrences_in_classified_sample"], reverse=True)


def mock_explanation(facts):
    """Safe local fallback: states only facts and conservative operator checks."""
    count = facts["occurrences_in_classified_sample"]
    template = facts["event_template"]
    return {
        "classification": "actionable",
        "reason": f"The same Apache error pattern was classified as actionable {count} time(s) in the evaluated sample.",
        "evidence": [
            f"Repeated template: {template}",
            f"Observed {count} actionable occurrence(s) from {facts['first_seen']} to {facts['last_seen']}.",
            *[f"Source log: {line}" for line in facts["representative_logs"][:2]],
        ],
        "likely_cause": "The log evidence indicates a worker/initialization failure, but does not establish a single root cause.",
        "suggested_checks": [
            "Inspect the Apache/mod_jk worker configuration and worker-process health.",
            "Correlate this time range with application and system logs before restarting services.",
        ],
        "confidence": "medium",
    }


def explain_with_gemini(facts, model_name):
    """Use Gemini only for wording/triage; factual evidence remains the supplied facts."""
    import google.generativeai as genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Set GEMINI_API_KEY, or run with --mock.")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)
    facts_json = json.dumps(facts, indent=2)
    prompt = f"""You are an SRE incident triage assistant. Produce one concise JSON object.

Use ONLY the factual observations below. Do not invent retries, services, hosts,
errors, or a confirmed root cause. A likely cause must be clearly phrased as a
hypothesis. Evidence must quote or restate only supplied observations.

Return exactly this JSON shape, with no markdown:
{{
  "classification": "actionable",
  "reason": "one sentence",
  "evidence": ["fact 1", "fact 2"],
  "likely_cause": "cautious hypothesis or insufficient evidence",
  "suggested_checks": ["safe check 1", "safe check 2"],
  "confidence": "low|medium|high"
}}

FACTS:
{facts_json}
"""
    text = model.generate_content(prompt).text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


def to_markdown(reports, is_mock):
    heading = "# Incident reports"
    if is_mock:
        heading += " — MOCK FORMAT ONLY"
    lines = [heading, ""]
    for index, report in enumerate(reports, 1):
        facts = report["facts"]
        explanation = report["report"]
        lines += [
            f"## Incident {index}: {facts['incident_key']}",
            "",
            f"**Classification:** {explanation['classification'].title()}",
            "",
            f"**Reason:** {explanation['reason']}",
            "",
            "**Evidence:**",
            *[f"- {item}" for item in explanation["evidence"]],
            "",
            f"**Likely cause:** {explanation['likely_cause']}",
            "",
            "**Suggested checks:**",
            *[f"- {item}" for item in explanation["suggested_checks"]],
            "",
            f"**Confidence:** {explanation['confidence'].title()}",
            "",
        ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", default=DEFAULT_PREDICTIONS_PATH)
    parser.add_argument("--logs", default=STRUCTURED_LOG_PATH)
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--label-column", default="auto",
                        help="label column to use; auto prefers verified_label, then predicted_label")
    parser.add_argument("--mock", action="store_true", help="local report formatting only; no Gemini call")
    parser.add_argument("--model", default="gemini-3.5-flash")
    args = parser.parse_args()

    logs_by_id, all_logs = load_logs(args.logs)
    actionable = load_actionable_predictions(args.predictions, logs_by_id, args.label_column)
    facts = build_incident_facts(actionable, all_logs)
    if not facts:
        print("No actionable predictions found; no incident report created.", file=sys.stderr)
        sys.exit(1)

    reports = []
    for fact in facts:
        report = mock_explanation(fact) if args.mock else explain_with_gemini(fact, args.model)
        reports.append({"facts": fact, "report": report})

    payload = {"is_mock": args.mock, "prediction_source": args.predictions, "incidents": reports}
    with open(args.output, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)
    markdown_path = os.path.splitext(args.output)[0] + ".md"
    with open(markdown_path, "w", encoding="utf-8") as file:
        file.write(to_markdown(reports, args.mock))
    print(f"Created {len(reports)} incident report(s): {args.output}")
    print(f"Readable demo report: {markdown_path}")


if __name__ == "__main__":
    main()
