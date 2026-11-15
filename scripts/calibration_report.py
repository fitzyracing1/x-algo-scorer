"""Compare guessed engagement probabilities against real outcomes.

Reads every data/submissions/*.yml that has an `actual:` block with
impressions > 0, converts the real counts to per-impression rates, and lines
them up against the probabilities that were guessed when the post was scored.

The ratio column is the honest takeaway: if your favorite guesses average 3x
the real favorite rate, divide future guesses by ~3. This is deliberately
simple -- a sanity check for your own guesses, not a model. Per-impression
rates are also not the same thing as the per-viewer probabilities the real
ranker predicts; they are just the closest observable proxy.

  python scripts/calibration_report.py [--markdown] [--dir data/submissions]
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBMISSIONS_DIR = REPO_ROOT / "data" / "submissions"

# prediction head -> field name in the `actual:` block
HEAD_TO_ACTUAL = {
    "favorite": "favorites",
    "reply": "replies",
    "retweet": "reposts",
    "quote": "quotes",
}


def load_submissions(directory: Path) -> list[dict]:
    subs = []
    for path in sorted(directory.glob("*.yml")):
        record = yaml.safe_load(path.read_text())
        if isinstance(record, dict):
            record["_path"] = path.name
            subs.append(record)
    return subs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=str(SUBMISSIONS_DIR))
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args(argv)

    directory = Path(args.dir)
    subs = load_submissions(directory) if directory.is_dir() else []
    with_actuals = [
        s for s in subs
        if isinstance(s.get("actual"), dict) and s["actual"].get("impressions", 0) > 0
    ]

    md = args.markdown
    title = "Calibration report"
    print(f"## {title}" if md else f"{title}\n{'=' * len(title)}")
    print(f"{len(subs)} stored submission(s), {len(with_actuals)} with real outcomes recorded.")
    if not with_actuals:
        print(
            "\nNothing to calibrate yet. Edit a file in data/submissions/ and fill in "
            "the `actual:` block (impressions, favorites, replies, reposts, quotes) "
            "with the numbers the post really got."
        )
        return 0

    rows = []
    for head, field in HEAD_TO_ACTUAL.items():
        pairs = []
        for s in with_actuals:
            guessed = (s.get("predictions") or {}).get(head)
            count = s["actual"].get(field)
            if guessed is not None and count is not None:
                pairs.append((float(guessed), count / s["actual"]["impressions"]))
        if not pairs:
            continue
        mean_guess = statistics.mean(p for p, _ in pairs)
        mean_actual = statistics.mean(a for _, a in pairs)
        ratio = mean_guess / mean_actual if mean_actual > 0 else float("inf")
        rows.append((head, len(pairs), mean_guess, mean_actual, ratio))

    if not rows:
        print(
            "\nSubmissions with outcomes exist, but none of them included guessed "
            "probabilities for the calibratable heads "
            f"({', '.join(HEAD_TO_ACTUAL)}). Default guesses are not calibrated."
        )
        return 0

    print()
    if md:
        print("| head | n | mean guessed P | mean actual rate | guess / actual |")
        print("|---|---:|---:|---:|---:|")
        for head, n, g, a, r in rows:
            print(f"| {head} | {n} | {g:.5f} | {a:.5f} | {r:.2f}x |")
    else:
        print(f"{'head':<12} {'n':>3} {'mean guessed':>14} {'mean actual':>13} {'guess/actual':>13}")
        for head, n, g, a, r in rows:
            print(f"{head:<12} {n:>3} {g:>14.5f} {a:>13.5f} {r:>12.2f}x")

    print()
    worst = max(rows, key=lambda row: abs(row[4] - 1.0) if row[4] != float("inf") else 0)
    if worst[4] != float("inf") and abs(worst[4] - 1.0) > 0.25:
        direction = "high" if worst[4] > 1 else "low"
        print(
            f"Biggest miss: '{worst[0]}' guesses run ~{worst[4]:.1f}x {direction} "
            f"versus observed per-impression rates. Scale future guesses accordingly."
        )
    else:
        print("Guesses are within 25% of observed rates on every calibratable head. Not bad.")
    print(
        "\nReminder: per-impression rates observed on your posts are only a rough proxy "
        "for the per-viewer probabilities the real ranker predicts."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
