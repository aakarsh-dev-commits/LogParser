"""
Evaluate classifier predictions against manually-confirmed ground truth.

Usage:
    python3 evaluate.py --file data/predictions.csv               # real Gemini run
    python3 evaluate.py --file data/predictions_MOCK_DEMO_ONLY.csv # mock plumbing test
"""
import argparse
import csv
import json

from sklearn.metrics import accuracy_score, precision_recall_fscore_support, confusion_matrix

REPORT_PATH_TEMPLATE = "data/evaluation_report_{tag}.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="data/predictions.csv",
                         help="path to a predictions CSV produced by few_shot_classifier.py")
    args = parser.parse_args()
    predictions_path = args.file
    is_mock = "MOCK" in predictions_path.upper()

    with open(predictions_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    y_true = [r["true_label"] for r in rows]
    y_pred = [r["predicted_label"] for r in rows]

    labels = ["noise", "actionable"]
    acc = accuracy_score(y_true, y_pred)
    precision, recall, f1, support = precision_recall_fscore_support(
        y_true, y_pred, labels=labels, zero_division=0
    )
    cm = confusion_matrix(y_true, y_pred, labels=labels)

    print(f"n = {len(rows)}")
    if is_mock:
        print("*** MOCK RUN -- these are NOT real Gemini results, plumbing test only ***")
    print(f"Accuracy: {acc:.3f}\n")
    print(f"{'label':<12}{'precision':<12}{'recall':<12}{'f1':<12}{'support'}")
    for i, label in enumerate(labels):
        print(f"{label:<12}{precision[i]:<12.3f}{recall[i]:<12.3f}{f1[i]:<12.3f}{support[i]}")

    print("\nConfusion matrix (rows=true, cols=predicted):")
    print(f"{'':<12}{'noise':<12}{'actionable'}")
    for i, label in enumerate(labels):
        print(f"{label:<12}{cm[i][0]:<12}{cm[i][1]}")

    # misclassified examples -- most useful thing to show in a demo
    wrong = [r for r in rows if r["true_label"] != r["predicted_label"]]
    print(f"\n{len(wrong)} misclassified examples (showing up to 5):")
    for r in wrong[:5]:
        print(f'  "{r["Content"]}" -- true={r["true_label"]}, predicted={r["predicted_label"]}')

    report = {
        "source_file": predictions_path,
        "is_mock": is_mock,
        "n": len(rows),
        "accuracy": acc,
        "per_label": {
            label: {"precision": precision[i], "recall": recall[i], "f1": f1[i], "support": int(support[i])}
            for i, label in enumerate(labels)
        },
        "confusion_matrix": {"labels": labels, "matrix": cm.tolist()},
        "misclassified_count": len(wrong),
    }
    report_path = REPORT_PATH_TEMPLATE.format(tag="mock_demo" if is_mock else "real")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\nFull report written to {report_path}")


if __name__ == "__main__":
    main()
