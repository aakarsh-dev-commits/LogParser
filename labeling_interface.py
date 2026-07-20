"""
Labeling interface for the log-noise classifier POC.

Design choice: we label at the TEMPLATE level (6 templates for Apache_2k),
not the raw-line level (2000 lines). Every raw line collapses onto one of
6 templates via Drain-style parsing (loghub already ships EventId/EventTemplate
for this dataset), so a human only needs to make 6 judgment calls to produce
a ground truth that covers all 2000 lines. This is standard practice in log
anomaly detection research (label templates, propagate to instances) and is
what makes "manually labeled ground truth" actually tractable in a hackathon
timeframe.

Run:
    python3 labeling_interface.py --review    # walk through and confirm/edit each template
    python3 labeling_interface.py --propagate # expand confirmed template labels to full dataset
"""
import argparse
import csv
import sys

TEMPLATE_GT_PATH = "data/template_ground_truth.csv"
STRUCTURED_LOG_PATH = "data/Apache_2k.log_structured.csv"
OUTPUT_GROUND_TRUTH_PATH = "data/ground_truth.csv"


def load_template_gt():
    with open(TEMPLATE_GT_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_template_gt(rows):
    fieldnames = ["EventId", "EventTemplate", "apache_level_tag", "proposed_label", "rationale", "human_confirmed"]
    with open(TEMPLATE_GT_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def review():
    rows = load_template_gt()
    print(f"\nReviewing {len(rows)} templates. For each: [y] confirm, [n] flip label, [q] quit & save.\n")
    for row in rows:
        print("-" * 70)
        print(f"EventId:        {row['EventId']}")
        print(f"Template:       {row['EventTemplate']}")
        print(f"Apache tag:     {row['apache_level_tag']}")
        print(f"Proposed label: {row['proposed_label'].upper()}")
        print(f"Rationale:      {row['rationale']}")
        choice = input("Confirm this label? [y/n/q]: ").strip().lower()
        if choice == "q":
            break
        elif choice == "n":
            row["proposed_label"] = "actionable" if row["proposed_label"] == "noise" else "noise"
            row["human_confirmed"] = "FLIPPED_BY_HUMAN"
            print(f"  -> flipped to {row['proposed_label'].upper()}")
        else:
            row["human_confirmed"] = "CONFIRMED"
    save_template_gt(rows)
    unconfirmed = [r["EventId"] for r in rows if r["human_confirmed"] == "PENDING"]
    if unconfirmed:
        print(f"\nStill pending review: {unconfirmed}")
    else:
        print("\nAll templates reviewed and saved.")


def propagate():
    """Expand confirmed template-level labels to every line in the dataset."""
    template_rows = load_template_gt()
    still_pending = [r["EventId"] for r in template_rows if r["human_confirmed"] == "PENDING"]
    if still_pending:
        print(f"WARNING: templates {still_pending} were never confirmed by a human reviewer.")
        print("Run --review first, or proceed knowingly (Claude-proposed labels only).")

    label_by_event = {r["EventId"]: r["proposed_label"] for r in template_rows}

    with open(STRUCTURED_LOG_PATH, newline="", encoding="utf-8") as f_in, \
         open(OUTPUT_GROUND_TRUTH_PATH, "w", newline="", encoding="utf-8") as f_out:
        reader = csv.DictReader(f_in)
        writer = csv.writer(f_out)
        writer.writerow(["LineId", "Time", "Content", "EventId", "label"])
        n = 0
        for row in reader:
            label = label_by_event.get(row["EventId"], "UNKNOWN")
            writer.writerow([row["LineId"], row["Time"], row["Content"], row["EventId"], label])
            n += 1
    print(f"Wrote {n} labeled lines to {OUTPUT_GROUND_TRUTH_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", action="store_true", help="Interactively review/confirm template labels")
    parser.add_argument("--propagate", action="store_true", help="Expand confirmed labels to all 2000 lines")
    args = parser.parse_args()

    if not args.review and not args.propagate:
        parser.print_help()
        sys.exit(0)
    if args.review:
        review()
    if args.propagate:
        propagate()
