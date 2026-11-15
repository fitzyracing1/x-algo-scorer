"""Fetch real engagement for stored submissions from the X API v2.

For every data/submissions/*.yml that carries a post URL (or tweet_id), this
looks up public_metrics via GET /2/tweets?ids=...&tweet.fields=public_metrics,
created_at, batching up to 100 ids per call. Each run appends a timestamped
snapshot to data/metrics/<submission-id>.yml and copies the newest counts into
the submission's `actual:` block, which is what the calibration report reads.
Manual "likes: N" comments on the issue remain a fallback and are simply
overwritten by fresher API data when it arrives.

Auth is a bearer token in the X_BEARER_TOKEN environment variable (in Actions,
a repository secret). Without it the script skips gracefully: exit code 0 and
a clear message, so the scheduled workflow stays green on forks and repos
without the secret. The token is only ever placed in the Authorization header,
never printed.

Scheduling: run daily. A submission is fetched while it is younger than
--max-age-days (default 30) and its last snapshot is older than --min-gap-hours
(default 20), so a daily cron naturally produces the ~24h and ~72h snapshots.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBMISSIONS_DIR = REPO_ROOT / "data" / "submissions"
METRICS_DIR = REPO_ROOT / "data" / "metrics"

API_URL = "https://api.x.com/2/tweets"
BATCH_SIZE = 100  # documented maximum for the ids parameter
MAX_RATE_LIMIT_WAIT = 300  # seconds we are willing to sleep on a 429

TWEET_URL_RE = re.compile(r"(?:twitter|x)\.com/[^/]+/status(?:es)?/(\d+)")

# public_metrics field -> our `actual:` field
METRIC_TO_ACTUAL = {
    "like_count": "favorites",
    "reply_count": "replies",
    "retweet_count": "reposts",
    "quote_count": "quotes",
    "bookmark_count": "bookmarks",
    "impression_count": "impressions",
}


def tweet_id_of(record: dict) -> str | None:
    explicit = record.get("tweet_id")
    if explicit:
        return str(explicit)
    m = TWEET_URL_RE.search(record.get("url") or "")
    return m.group(1) if m else None


def parse_ts(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def default_http_get(url: str, token: str) -> tuple[int, dict, bytes]:
    """Returns (status, lowercased headers, body). Token goes only in the header."""
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "User-Agent": "x-algo-scorer-actuals-fetcher"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def fetch_batch(ids: list[str], token: str, http_get, sleep=time.sleep) -> dict:
    """One /2/tweets lookup for <=100 ids, with a single bounded 429 retry."""
    assert len(ids) <= BATCH_SIZE
    query = urllib.parse.urlencode(
        {"ids": ",".join(ids), "tweet.fields": "public_metrics,created_at"}
    )
    url = f"{API_URL}?{query}"
    for attempt in (1, 2):
        status, headers, body = http_get(url, token)
        if status == 429 and attempt == 1:
            reset = headers.get("x-rate-limit-reset")
            retry_after = headers.get("retry-after")
            wait = 60.0
            if retry_after:
                wait = float(retry_after)
            elif reset:
                wait = float(reset) - time.time()
            wait = max(1.0, min(wait, MAX_RATE_LIMIT_WAIT))
            print(f"rate limited (429); waiting {wait:.0f}s before one retry")
            sleep(wait)
            continue
        if status == 429:
            raise RuntimeError(
                "still rate limited after one retry; giving up for this run "
                "(the next scheduled run will pick these up)"
            )
        if status != 200:
            # body may contain error details but never the token
            raise RuntimeError(f"X API returned HTTP {status}: {body[:300].decode(errors='replace')}")
        return json.loads(body)
    raise AssertionError("unreachable")


def collect_targets(now: datetime.datetime, submissions_dir: Path, metrics_dir: Path,
                    max_age_days: float, min_gap_hours: float, force: bool) -> list[dict]:
    targets = []
    for path in sorted(submissions_dir.glob("*.yml")):
        record = yaml.safe_load(path.read_text())
        if not isinstance(record, dict):
            continue
        tid = tweet_id_of(record)
        if not tid:
            continue
        sub_id = record.get("id") or path.stem
        age_days = None
        if record.get("submitted_at"):
            age_days = (now - parse_ts(record["submitted_at"])).total_seconds() / 86400
        if not force:
            if age_days is not None and age_days > max_age_days:
                continue
            snap_path = metrics_dir / f"{sub_id}.yml"
            if snap_path.exists():
                snaps = yaml.safe_load(snap_path.read_text()).get("snapshots") or []
                if snaps:
                    last = parse_ts(snaps[-1]["fetched_at"])
                    if (now - last).total_seconds() / 3600 < min_gap_hours:
                        continue
        targets.append({"submission_id": sub_id, "tweet_id": tid, "path": path,
                        "record": record})
    return targets


def append_snapshot(metrics_dir: Path, target: dict, metrics: dict,
                    now: datetime.datetime, status: str) -> dict:
    snap_path = metrics_dir / f"{target['submission_id']}.yml"
    doc = {"submission": target["submission_id"], "tweet_id": target["tweet_id"],
           "snapshots": []}
    if snap_path.exists():
        doc = yaml.safe_load(snap_path.read_text())
        doc.setdefault("snapshots", [])
    snapshot = {"fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": status}
    submitted = target["record"].get("submitted_at")
    if submitted:
        snapshot["age_hours"] = round(
            (now - parse_ts(submitted)).total_seconds() / 3600, 1
        )
    snapshot.update(metrics)
    doc["snapshots"].append(snapshot)
    metrics_dir.mkdir(parents=True, exist_ok=True)
    snap_path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return snapshot


def update_submission_actuals(target: dict, metrics: dict, now: datetime.datetime):
    record = target["record"]
    record.setdefault("actual", {})
    for metric, field in METRIC_TO_ACTUAL.items():
        if metric in metrics:
            record["actual"][field] = metrics[metric]
    record["actual"]["recorded_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    record["actual"]["source"] = "x-api-v2"
    target["path"].write_text(yaml.safe_dump(record, sort_keys=False, allow_unicode=True))


def run(token: str | None, submissions_dir: Path, metrics_dir: Path,
        max_age_days: float, min_gap_hours: float, force: bool,
        http_get=default_http_get, sleep=time.sleep,
        now: datetime.datetime | None = None) -> dict:
    """Returns a summary dict: {skipped, fetched, errors, messages}."""
    summary = {"skipped": False, "fetched": 0, "errors": [], "messages": []}

    def say(msg):
        summary["messages"].append(msg)
        print(msg)

    if not token:
        summary["skipped"] = True
        say(
            "X_BEARER_TOKEN is not set; skipping the X API fetch. Set the "
            "repository secret X_BEARER_TOKEN (see README) to enable automatic "
            "actuals. Manual 'likes: N, replies: N, ...' issue comments still work."
        )
        return summary

    now = now or datetime.datetime.now(datetime.timezone.utc)
    targets = collect_targets(now, submissions_dir, metrics_dir,
                              max_age_days, min_gap_hours, force)
    if not targets:
        say("No submissions due for a metrics fetch (no post URLs, too old, or "
            "snapshotted recently).")
        return summary

    by_tweet = {t["tweet_id"]: t for t in targets}
    ids = list(by_tweet)
    say(f"Fetching public_metrics for {len(ids)} post(s) in batches of {BATCH_SIZE}.")

    for i in range(0, len(ids), BATCH_SIZE):
        batch = ids[i:i + BATCH_SIZE]
        if i:
            sleep(2)  # be polite between batches
        try:
            payload = fetch_batch(batch, token, http_get, sleep=sleep)
        except RuntimeError as e:
            summary["errors"].append(str(e))
            say(f"batch failed: {e}")
            break

        for tweet in payload.get("data", []):
            target = by_tweet[tweet["id"]]
            metrics = dict(tweet.get("public_metrics") or {})
            if tweet.get("created_at"):
                metrics["tweet_created_at"] = tweet["created_at"]
            append_snapshot(metrics_dir, target, metrics, now, status="ok")
            update_submission_actuals(target, metrics, now)
            summary["fetched"] += 1
            say(f"  {target['submission_id']}: " + ", ".join(
                f"{k}={v}" for k, v in metrics.items() if k.endswith("_count")))
        for err in payload.get("errors", []):
            tid = err.get("resource_id") or err.get("value")
            target = by_tweet.get(str(tid))
            detail = err.get("detail") or err.get("title") or "unknown error"
            if target:
                append_snapshot(metrics_dir, target, {"error": detail}, now,
                                status="error")
            summary["errors"].append(f"{tid}: {detail}")
            say(f"  {tid}: not available ({detail})")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-days", type=float, default=30)
    parser.add_argument("--min-gap-hours", type=float, default=20)
    parser.add_argument("--force", action="store_true",
                        help="fetch every submission with a tweet id, regardless of age/gap")
    parser.add_argument("--submissions-dir", default=str(SUBMISSIONS_DIR))
    parser.add_argument("--metrics-dir", default=str(METRICS_DIR))
    args = parser.parse_args(argv)

    summary = run(
        token=os.environ.get("X_BEARER_TOKEN"),
        submissions_dir=Path(args.submissions_dir),
        metrics_dir=Path(args.metrics_dir),
        max_age_days=args.max_age_days,
        min_gap_hours=args.min_gap_hours,
        force=args.force,
    )

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"skipped={'true' if summary['skipped'] else 'false'}\n")
            f.write(f"fetched={summary['fetched']}\n")
            f.write("report<<EOF\n" + "\n".join(summary["messages"]) + "\nEOF\n")
    # Errors for individual tweets (deleted posts etc.) should not fail the run.
    return 0


if __name__ == "__main__":
    sys.exit(main())
