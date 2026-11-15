"""Score a submitted post and optionally store it as a data file.

Used by the "Score a post" GitHub Actions workflow, but runs fine locally:

  python scripts/score_submission.py --text "hello" \
      --predictions '{"favorite": 0.05, "reply": 0.004}' --save

Saved submissions live in data/submissions/<id>.yml. Later, edit the file and
add an `actual:` block with the real engagement the post got (impressions,
favorites, replies, reposts, quotes); scripts/calibration_report.py then
compares guessed probabilities with reality.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xscore.scorer import format_breakdown, load_config, load_post, score_post

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBMISSIONS_DIR = REPO_ROOT / "data" / "submissions"


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]
    return slug or "post"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", help="post text")
    parser.add_argument("--file", help="existing post/submission YAML or JSON file to (re)score")
    parser.add_argument("--predictions", default="",
                        help='JSON object of guessed probabilities, e.g. {"favorite": 0.05}')
    parser.add_argument("--flags", default="",
                        help='JSON object of flags, e.g. {"is_mutual_follow_author": true}')
    parser.add_argument("--save", action="store_true", help="store as data/submissions/<id>.yml")
    parser.add_argument("--url", default="",
                        help="post URL on X; enables automatic actuals fetching later")
    parser.add_argument("--id", help="submission id (default: date + text slug)")
    parser.add_argument("--markdown", action="store_true", help="emit markdown breakdown")
    args = parser.parse_args(argv)

    if bool(args.text) == bool(args.file):
        parser.error("provide exactly one of --text or --file")

    if args.file:
        post = load_post(args.file)
    else:
        post = {"text": args.text}
    if args.predictions.strip():
        post["predictions"] = {**(post.get("predictions") or {}),
                               **json.loads(args.predictions)}
    if args.flags.strip():
        post["flags"] = {**(post.get("flags") or {}), **json.loads(args.flags)}

    config = load_config()
    result = score_post(post, config)
    print(format_breakdown(result, config, markdown=args.markdown))

    if args.save:
        now = datetime.datetime.now(datetime.timezone.utc)
        sub_id = args.id or f"{now:%Y%m%d-%H%M%S}-{slugify(result.text)}"
        record = {
            "id": sub_id,
            "submitted_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "url": args.url or post.get("url", ""),
            "text": result.text,
            "predictions": post.get("predictions") or {},
            "flags": post.get("flags") or {},
            "score": {
                "weighted_sum": round(result.weighted_sum, 8),
                "final_score": round(result.final_score, 8),
                "config_commit": config["source"]["commit"],
            },
            # Fill this in later with the real numbers your post got, then run
            # scripts/calibration_report.py. Set impressions > 0 to be counted.
            "actual": {
                "impressions": 0,
                "favorites": 0,
                "replies": 0,
                "reposts": 0,
                "quotes": 0,
            },
        }
        if "actual" in post:
            record["actual"] = post["actual"]
        SUBMISSIONS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = SUBMISSIONS_DIR / f"{sub_id}.yml"
        out_path.write_text(yaml.safe_dump(record, sort_keys=False, allow_unicode=True))
        print(f"\nsaved submission: {out_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
