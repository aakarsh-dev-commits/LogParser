"""
Few-shot log-noise classifier using the Gemini API.

Requires: pip install google-generativeai
Requires: export GEMINI_API_KEY=your_key_here

Usage:
    python3 few_shot_classifier.py --sample 200            # real Gemini calls
    python3 few_shot_classifier.py --sample 200 --mock     # no API key needed, tests plumbing only

    # Phase 2 — classify only the LLM-routed subset:
    python3 few_shot_classifier.py --input data/routed_for_llm.csv --sample 200

IMPORTANT: --mock uses a dumb keyword rule, NOT Gemini. It exists so the
pipeline (batching, JSON parsing, output format, evaluation) can be verified
end-to-end without an API key. Never present --mock numbers as the model's
real classification performance.
"""
import argparse
import csv
import json
import os
import random
import sys
import time

GROUND_TRUTH_PATH = "data/ground_truth.csv"
BATCH_SIZE = 20

# Few-shot exemplars: hand-picked, held out from evaluation, one per template
# so the model sees the diversity of what "noise" and "actionable" look like
# rather than just two near-duplicate examples.
FEW_SHOT_EXAMPLES = [
    ("jk2_init() Found child 6725 in scoreboard slot 10", "noise",
     "routine worker registration at startup, no action needed"),
    ("workerEnv.init() ok /etc/httpd/conf/workers2.properties", "noise",
     "routine confirmation that config loaded"),
    ("[client 222.166.160.184] Directory index forbidden by rule: /var/www/html/", "noise",
     "Apache correctly blocking directory listing per config; expected behavior, not a problem"),
    ("mod_jk child workerEnv in error state 6", "actionable",
     "explicit persistent error state on a worker, needs investigation"),
    ("jk2_init() Can't find child 1566 in scoreboard", "actionable",
     "worker registration is failing, a real operational problem"),
    ("mod_jk child init 1 -2", "actionable",
     "negative status code consistent with an internal failure"),
]

SYSTEM_PROMPT = """You are an SRE assistant classifying Apache web server log lines.
Classify each line as one of exactly two labels:
- "noise": routine, informational, or expected behavior. No operator action needed.
- "actionable": indicates a real operational problem worth an operator's attention.

Here are labeled examples:
""" + "\n".join(
    f'Log: "{line}"\nLabel: {label} ({reason})\n' for line, label, reason in FEW_SHOT_EXAMPLES
) + """
Now classify the following NEW log lines (they are NOT the examples above).
Respond with ONLY a JSON array of objects like [{"id": 1, "label": "noise"}, ...],
one object per numbered line below, no other text, no markdown fences.

Lines to classify:
"""


def load_sample(n, seed=42, input_path=None):
    path = input_path if input_path else GROUND_TRUTH_PATH
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    few_shot_texts = {ex[0] for ex in FEW_SHOT_EXAMPLES}
    pool = [r for r in rows if r["Content"] not in few_shot_texts]
    random.Random(seed).shuffle(pool)
    return pool[:n]


def call_gemini_batch(batch_lines, model_name="gemini-3.5-flash", max_retries=5):
    # NOTE (July 2026): gemini-2.0-flash's free tier was retired by Google.
    # gemini-3.5-flash's free tier is real but rate-limited to ~5 requests/minute --
    # this retries with backoff on 429s instead of crashing the whole run.
    import google.generativeai as genai
    from google.api_core.exceptions import ResourceExhausted

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: set GEMINI_API_KEY environment variable.", file=sys.stderr)
        sys.exit(1)
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(model_name)

    numbered = "\n".join(f'{i+1}. "{line}"' for i, line in enumerate(batch_lines))
    prompt = SYSTEM_PROMPT + numbered

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
            print(f"  rate limited, waiting {wait:.0f}s before retry...")
            time.sleep(wait)
            wait = min(wait * 1.5, 60)


def call_mock_batch(batch_lines):
    """Dumb keyword rule -- for testing the pipeline plumbing only."""
    labels = []
    for line in batch_lines:
        if "error state" in line or "Can't find" in line or ("init" in line and "-2" in line):
            labels.append("actionable")
        else:
            labels.append("noise")
    return labels


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=200, help="number of lines to classify")
    parser.add_argument("--mock", action="store_true", help="use dumb local rule instead of Gemini")
    parser.add_argument(
        "--input",
        default=None,
        help=(
            "Path to a log CSV with a 'Content' column (e.g. data/routed_for_llm.csv "
            "from routing.py). Defaults to data/ground_truth.csv."
        ),
    )
    args = parser.parse_args()

    input_path = args.input if args.input else GROUND_TRUTH_PATH
    if not os.path.exists(input_path):
        if args.input:
            print(f"ERROR: {input_path} not found. Run: python3 routing.py first.")
        else:
            print(f"ERROR: {input_path} not found. Run: python3 labeling_interface.py --propagate")
        sys.exit(1)

    predictions_path = "data/predictions_MOCK_DEMO_ONLY.csv" if args.mock else "data/predictions.csv"

    sample = load_sample(args.sample, input_path=input_path)
    print(f"Classifying {len(sample)} lines in batches of {BATCH_SIZE} "
          f"({'MOCK -- not real Gemini output' if args.mock else 'via Gemini API'})...")

    all_preds = []
    for i in range(0, len(sample), BATCH_SIZE):
        batch = sample[i:i + BATCH_SIZE]
        lines = [r["Content"] for r in batch]
        if args.mock:
            labels = call_mock_batch(lines)
        else:
            labels = call_gemini_batch(lines)
            time.sleep(13)  # be polite to the API
        for row, label in zip(batch, labels):
            all_preds.append({"LineId": row["LineId"], "Content": row["Content"],
                               "true_label": row["label"], "predicted_label": label})
        print(f"  batch {i // BATCH_SIZE + 1}/{(len(sample) - 1) // BATCH_SIZE + 1} done")

    with open(predictions_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["LineId", "Content", "true_label", "predicted_label"])
        w.writeheader()
        w.writerows(all_preds)

    print(f"Wrote {len(all_preds)} predictions to {predictions_path}")


if __name__ == "__main__":
    main()
