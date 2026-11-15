"""Bot behind the "Score my post" issue flow.

Driven by the issue-scoring workflow with the raw GitHub event payload:

* issue opened with the score-request label -> parse the issue form, score the
  post, store data/submissions/issue-<N>.yml, and emit the breakdown comment.
* issue comment like "likes: 120, replies: 6, reposts: 14" -> store the real
  numbers in that submission's `actual:` block and emit a comparison comment.

The script only writes files (submission YAML and the comment body at --out);
the workflow does the committing and the actual comment posting, so this stays
testable offline.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xscore.scorer import format_breakdown, load_config, score_post

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data" / "submissions"

FORM_FIELDS = {
    "Post URL": "url",
    "Post text": "text",
    "Guessed probabilities (JSON, optional)": "probabilities",
}

# "likes: 120, replies: 6" style pairs in a comment -> actual: fields
ACTUAL_ALIASES = {
    "like": "favorites", "likes": "favorites",
    "favorite": "favorites", "favorites": "favorites", "favs": "favorites",
    "reply": "replies", "replies": "replies",
    "repost": "reposts", "reposts": "reposts",
    "retweet": "reposts", "retweets": "reposts",
    "quote": "quotes", "quotes": "quotes",
    "impression": "impressions", "impressions": "impressions",
    "view": "impressions", "views": "impressions",
}
ACTUAL_RE = re.compile(r"\b([a-zA-Z]+)\s*[:=]\s*([\d][\d,]*)\b")

ACTUALS_HINT = (
    "When the post has real numbers, comment here like: "
    "`likes: 120, replies: 6, reposts: 14, quotes: 2, impressions: 40000` "
    "and I will store them for calibration (impressions are needed to compute rates)."
)


def parse_issue_form(body: str) -> dict:
    """Parse a GitHub issue-form body (### <label> sections) into a dict."""
    sections: dict[str, str] = {}
    current = None
    lines: list[str] = []
    for line in (body or "").splitlines():
        if line.startswith("### "):
            if current:
                sections[current] = "\n".join(lines).strip()
            current = line[4:].strip()
            lines = []
        else:
            lines.append(line)
    if current:
        sections[current] = "\n".join(lines).strip()

    out = {}
    for label, key in FORM_FIELDS.items():
        value = sections.get(label, "")
        value = re.sub(r"^```[a-zA-Z]*\n?|```$", "", value.strip()).strip()
        if value and value != "_No response_":
            out[key] = value
    if "probabilities" in out:
        try:
            out["probabilities"] = json.loads(out["probabilities"])
        except json.JSONDecodeError as e:
            raise ValueError(f"could not parse the probabilities JSON: {e}") from e
        if not isinstance(out["probabilities"], dict):
            raise ValueError("probabilities must be a JSON object")
    return out


def parse_actuals(text: str) -> dict[str, int]:
    """Extract engagement counts from a free-form comment."""
    found = {}
    for key, num in ACTUAL_RE.findall(text or ""):
        field = ACTUAL_ALIASES.get(key.lower())
        if field:
            found[field] = int(num.replace(",", ""))
    return found


def submission_path(data_dir: Path, issue_number: int) -> Path:
    return data_dir / f"issue-{issue_number}.yml"


def handle_opened(issue: dict, data_dir: Path) -> str:
    form = parse_issue_form(issue.get("body", ""))
    if "text" not in form:
        return (
            "I could not find any **Post text** in this issue. Please open a new "
            "issue with the *Score my post* form and fill in the post text."
        )

    config = load_config()
    post = {"text": form["text"]}
    if "probabilities" in form:
        post["predictions"] = form["probabilities"]
    result = score_post(post, config)

    now = datetime.datetime.now(datetime.timezone.utc)
    record = {
        "id": f"issue-{issue['number']}",
        "issue": issue["number"],
        "submitted_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "url": form.get("url", ""),
        "text": form["text"],
        "predictions": post.get("predictions") or {},
        "flags": {},
        "score": {
            "weighted_sum": round(result.weighted_sum, 8),
            "final_score": round(result.final_score, 8),
            "config_commit": config["source"]["commit"],
        },
        "actual": {"impressions": 0, "favorites": 0, "replies": 0,
                   "reposts": 0, "quotes": 0},
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    submission_path(data_dir, issue["number"]).write_text(
        yaml.safe_dump(record, sort_keys=False, allow_unicode=True)
    )

    breakdown = format_breakdown(result, config, markdown=True)
    return f"{breakdown}\n\n---\n\n{ACTUALS_HINT}"


def handle_comment(issue: dict, comment: dict, data_dir: Path) -> str | None:
    """Returns the reply body, or None when the comment is not an actuals report."""
    actuals = parse_actuals(comment.get("body", ""))
    if not actuals:
        return None

    path = submission_path(data_dir, issue["number"])
    if not path.exists():
        return (
            "I found engagement numbers in your comment, but there is no stored "
            "submission for this issue, so I have nothing to attach them to."
        )

    record = yaml.safe_load(path.read_text())
    record.setdefault("actual", {})
    record["actual"].update(actuals)
    record["actual"]["recorded_at"] = datetime.datetime.now(
        datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(yaml.safe_dump(record, sort_keys=False, allow_unicode=True))

    lines = ["Stored real engagement for this submission:", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(actuals.items())]
    lines.append("")

    impressions = record["actual"].get("impressions", 0)
    predictions = record.get("predictions") or {}
    head_to_field = {"favorite": "favorites", "reply": "replies",
                     "retweet": "reposts", "quote": "quotes"}
    if impressions and predictions:
        lines.append("| head | you guessed | observed rate | guess / observed |")
        lines.append("|---|---:|---:|---:|")
        for head, field in head_to_field.items():
            if head in predictions and field in record["actual"]:
                guessed = float(predictions[head])
                observed = record["actual"][field] / impressions
                ratio = f"{guessed / observed:.2f}x" if observed > 0 else "inf"
                lines.append(f"| {head} | {guessed:g} | {observed:.5f} | {ratio} |")
        lines.append("")
        lines.append(
            "Observed per-impression rates are only a rough proxy for the "
            "per-viewer probabilities the real ranker predicts."
        )
    elif not impressions:
        lines.append(
            "No impressions recorded yet, so I cannot compute observed rates. "
            "Add `impressions: N` (views) in another comment to enable comparison."
        )
    lines.append("")
    lines.append(
        "Run `python scripts/calibration_report.py` on the repo for the picture "
        "across all submissions."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True, help="path to the GitHub event JSON")
    parser.add_argument("--out", required=True, help="where to write the comment body")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    args = parser.parse_args(argv)

    event = json.loads(Path(args.event).read_text())
    data_dir = Path(args.data_dir)
    issue = event["issue"]

    try:
        if "comment" in event:
            body = handle_comment(issue, event["comment"], data_dir)
        else:
            body = handle_opened(issue, data_dir)
    except ValueError as e:
        body = f"Could not score this submission: {e}"

    if body is not None:
        Path(args.out).write_text(body)
        print(f"wrote comment body to {args.out}")
    else:
        print("comment is not an actuals report; nothing to do")

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"has_comment={'true' if body is not None else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
