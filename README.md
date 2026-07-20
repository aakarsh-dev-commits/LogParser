# Log Noise Classifier — Phase 1 Proof of Concept

Built for Synergy 2026, Problem 04: *Log Noise Classifier Using Few-Shot Learning*.

## What this POC actually does

```
Apache_2k logs (loghub)
        │
        ▼
Drain-style template extraction   (loghub already ships EventId/EventTemplate for
        │                          this dataset — 2000 lines collapse to 6 templates)
        ▼
Human-reviewed template labeling  (labeling_interface.py — 6 judgment calls, not 2000)
        │
        ▼
Ground truth propagated to all 2000 lines
        │
        ▼
Few-shot Gemini classification (few_shot_classifier.py)
        │
        ▼
Evaluation: accuracy / precision / recall / F1 / confusion matrix (evaluate.py)
```

## Make the demo more useful: incident reports with evidence

A classifier alone only says *which* line needs attention. The POC also now
has an incident-reporting step that groups actionable predictions by template,
counts their recurrence, attaches representative source logs and asks Gemini
to write a cautious operator-facing report:

```text
Few-shot classification
        │
        ▼
Group actionable lines into incident candidates
        │
        ▼
Calculate evidence locally (counts, time range, source log lines)
        │
        ▼
Gemini writes a concise incident report + safe checks
```

Run it after creating real predictions:

```bash
python3 incident_reporter.py --predictions data/predictions.csv
```

It writes `data/incident_reports.json` for the application and
`data/incident_reports.md` for a slide/demo. The prompt explicitly prohibits
inventing evidence. For example, this Apache dataset can support a statement
about repeated `mod_jk` worker errors; it **cannot** support a claim about a
NameNode or failed retries, because those logs are not in this dataset.

`--mock` is available solely to test the grouping and report format without an
API key. Its output is clearly marked MOCK and must not be presented as Gemini
output.

## Review predictions before using them in a demo

Use the small Streamlit interface to inspect each model prediction next to the
ground truth. You can confirm or change the final label, and only verified
actionable entries need to be included in the final incident report:

```bash
pip install streamlit
streamlit run review_predictions.py
```

In the browser, choose **Predicted actionable**, review those rows and click
**Save verified review**. Then create reports from the reviewed file:

```bash
python3 incident_reporter.py --predictions data/verified_predictions.csv --label-column verified_label
```

This makes the demo honest and easy to explain: *the LLM proposes a label; a
human verifies the evaluation/demo set; verified actionable logs are grouped
into evidence-backed incident reports.*

## Why template-level labeling, and why it isn't just Apache's [notice]/[error] tag

Apache_2k has only **6 distinct log templates**. Every one of the 2000 raw
lines is an instance of one of these 6 templates, so a human reviewer only
needs to make 6 noise/actionable judgment calls — not 2000 — to produce a
ground truth that covers the whole dataset. That's what `labeling_interface.py`
is for: it's a genuine (if minimal) human-in-the-loop labeling step, which is
what the problem statement explicitly asks for ("design a labeling interface,"
"evaluate against a manually labeled ground truth").

We deliberately did **not** just copy Apache's built-in `[notice]`/`[error]`
severity tag as ground truth. See `data/template_ground_truth.csv` — one
template (`E4`, "Directory index forbidden by rule") is tagged `[error]` by
Apache itself but was reasoned as **noise** (it's the server correctly
blocking directory listing per config — expected behavior, nothing to fix).
The other three `[error]`-tagged templates were reasoned as **actionable**.
This shows independent judgment rather than reusing a label Apache already
wrote into the log line, which matters because raw severity tags conflate
"the software calls this an error" with "an operator should act on this."

**Before presenting:** have a teammate actually run
`python3 labeling_interface.py --review` and eyeball the 6 rationales (2
minutes). Right now they're pre-filled with Claude's reasoning — spot-check
and adjust anything you disagree with so you can honestly call it
human-confirmed.

## Quick start — no API key required

```bash
git clone https://github.com/YOUR-USERNAME/log-noise-classifier.git
cd log-noise-classifier
python3 -m pip install -r requirements.txt

# Runs the full classification/reporting plumbing with a local mock rule.
# This is only a demo of the workflow, not real LLM performance.
python3 few_shot_classifier.py --sample 50 --mock
python3 incident_reporter.py \
  --predictions data/predictions_MOCK_DEMO_ONLY.csv \
  --mock \
  --output data/incident_reports_mock.json

# Opens the human review dashboard.
streamlit run review_predictions.py
```

The mock outputs are deliberately named and labelled **MOCK**. Do not present
their evaluation metrics or incident reports as Gemini results.

## Run with Gemini

```bash
python3 -m pip install -r requirements.txt

# 1. Review and confirm the 6 template-level labels (edit if you disagree)
python3 labeling_interface.py --review

# 2. Propagate confirmed labels to all 2000 lines
python3 labeling_interface.py --propagate

# 3. Classify a sample using Gemini few-shot (needs an API key)
export GEMINI_API_KEY=your_key_here
python3 few_shot_classifier.py --sample 200

# 4. Evaluate
python3 evaluate.py
```

There's also a `--mock` flag on step 3 (`python3 few_shot_classifier.py --sample 200 --mock`)
that runs the exact same pipeline with a dumb keyword rule instead of Gemini —
**this exists only to prove the plumbing works without an API key**, and it
scores ~100% by construction (the rule mirrors the labeling logic). Never
present mock numbers as the model's real performance — only numbers from an
actual Gemini run belong on a slide.

## What's built now vs. the final vision

This POC now covers classification plus an evidence-grounded incident-reporting
vertical slice. Frequency routing and a production-quality incident clusterer
remain Phase 2 work:

```
[ BUILT NOW — this repo ]
Raw Logs → Template parsing → Few-shot LLM classification → Evidence-grounded incident report
                                                                    │
                                                                    ▼
[ PHASE 2 — final round ]
Frequency-aware routing → Incident clustering across time/services → richer root-cause analysis
                                                                    │
                                                                    ▼
                                                              Dashboard + evaluation
```
Frequency analysis is the natural next addition: it identifies *known,
routine* high-volume templates for bypassing the LLM so scarce API budget
is spent on genuinely ambiguous logs.

## Phase 2 — Efficiency Layer (now built)

```
Raw Logs
    |
    v
drain_parser.py        -- re-runs Drain on raw Content; produces data/drain_templates.csv
    |
    v
frequency_analyzer.py  -- counts occurrences per template, buckets into high/medium/low
    |
    v
routing.py             -- bypasses LLM for high-volume templates; routes the rest
    |
    v
few_shot_classifier.py --input data/routed_for_llm.csv  (only ~3% of logs for Apache_2k)
```

### Quick start — Phase 2

```bash
# 1. Install the new dependency
python3 -m pip install -r requirements.txt

# 2. Extract templates with Drain (independent of loghub's pre-parsed columns)
python3 drain_parser.py --compare

# 3. Analyse template frequencies and categorise
python3 frequency_analyzer.py --threshold-high 100

# 4. Route: bypass high-volume templates, forward the rest
python3 routing.py --threshold 100

# 5. Classify only the routed subset with Gemini
export GEMINI_API_KEY=your_key_here
python3 few_shot_classifier.py --input data/routed_for_llm.csv --sample 200

# 6. Evaluate and report (existing steps — unchanged)
python3 evaluate.py
python3 incident_reporter.py --predictions data/predictions.csv
```

The Streamlit review dashboard (`streamlit run review_predictions.py`) now shows
a **Phase 2 — Frequency-Aware LLM Routing** panel at the top when
`data/routing_stats.json` is present, with live bypass/route counts and
actual API savings percentages.

### Apache_2k routing results (default threshold = 100)

| Template | Occurrences | Category | Action |
|---|---|---|---|
| jk2_init() Found child <\*> in scoreboard slot <\*> | 836 | high_volume | Bypass |
| workerEnv.init() ok <\*> | 569 | high_volume | Bypass |
| mod_jk child workerEnv in error state <\*> | 539 | high_volume | Bypass |
| [client <\*>] Directory index forbidden … | 32 | medium_volume | LLM |
| jk2_init() Can't find child <\*> in scoreboard | 12 | medium_volume | LLM |
| mod_jk child init 1 -2 | 12 | medium_volume | LLM |

**97.2% of log lines bypass the LLM** — only 56 of 2000 lines are forwarded.


## Files

**Phase 1 (original)**

- `data/Apache_2k.log_structured.csv` — raw dataset (loghub, MIT-style license, public)
- `data/template_ground_truth.csv` — the 6 template-level labels + rationale (human-reviewable)
- `data/ground_truth.csv` — labels propagated to all 2000 lines
- `data/predictions_MOCK_DEMO_ONLY.csv` — clearly marked mock sample output
- `data/incident_reports_mock.md` — clearly marked mock incident-report sample
- `labeling_interface.py` — CLI labeling/review tool
- `few_shot_classifier.py` — Gemini few-shot classifier (+ mock mode; now accepts `--input`)
- `incident_reporter.py` — evidence-grounded incident grouping and Gemini report writer
- `review_predictions.py` — browser review screen (now includes Phase 2 routing summary panel)
- `evaluate.py` — accuracy / precision / recall / F1 / confusion matrix

**Phase 2 (new — efficiency layer)**

- `drain_parser.py` — re-runs Drain algorithm on raw Content; produces `data/drain_templates.csv`
- `frequency_analyzer.py` — frequency-based analysis; produces `data/frequency_report.csv`
- `routing.py` — frequency-aware LLM routing; produces `data/routed_for_llm.csv`, `data/bypassed_logs.csv`, `data/routing_stats.json`
