"""Fetcher logic with a mocked X API: batching, snapshots, 429s, graceful skip."""

import datetime
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_actuals
from fetch_actuals import collect_targets, fetch_batch, run, tweet_id_of

NOW = datetime.datetime(2026, 11, 16, 12, 0, tzinfo=datetime.timezone.utc)


def write_submission(directory, name, **extra):
    directory.mkdir(parents=True, exist_ok=True)
    record = {"id": name, "submitted_at": "2026-11-15T12:00:00Z", "text": "post",
              "predictions": {"favorite": 0.03}, "actual": {"impressions": 0}}
    record.update(extra)
    (directory / f"{name}.yml").write_text(yaml.safe_dump(record, sort_keys=False))
    return record


def api_payload(*tweets, errors=()):
    return 200, {}, json.dumps({"data": list(tweets), "errors": list(errors)}).encode()


def test_tweet_id_extraction():
    assert tweet_id_of({"url": "https://x.com/fitzy/status/12345"}) == "12345"
    assert tweet_id_of({"url": "https://twitter.com/a/statuses/999?s=20"}) == "999"
    assert tweet_id_of({"tweet_id": 777}) == "777"
    assert tweet_id_of({"url": "https://example.com/nope"}) is None
    assert tweet_id_of({}) is None


def test_missing_token_skips_gracefully(tmp_path):
    summary = run(token=None, submissions_dir=tmp_path, metrics_dir=tmp_path / "m",
                  max_age_days=30, min_gap_hours=20, force=False)
    assert summary["skipped"] is True
    assert "X_BEARER_TOKEN is not set" in summary["messages"][0]
    assert "still work" in summary["messages"][0]


def test_fetch_updates_snapshots_and_actuals(tmp_path):
    subs, metrics = tmp_path / "subs", tmp_path / "metrics"
    write_submission(subs, "issue-1", url="https://x.com/fitzy/status/111")
    write_submission(subs, "issue-2", url="https://x.com/fitzy/status/222")
    write_submission(subs, "no-url")  # must be ignored

    def fake_get(url, token):
        assert token == "tok"
        assert "tweet.fields=public_metrics" in url.replace("%2C", ",")
        return api_payload(
            {"id": "111", "created_at": "2026-11-15T12:01:00.000Z",
             "public_metrics": {"like_count": 60, "reply_count": 7,
                                "retweet_count": 9, "quote_count": 1,
                                "bookmark_count": 5, "impression_count": 2500}},
            errors=[{"resource_id": "222", "detail": "deleted"}],
        )

    summary = run(token="tok", submissions_dir=subs, metrics_dir=metrics,
                  max_age_days=30, min_gap_hours=20, force=False,
                  http_get=fake_get, now=NOW)
    assert summary["fetched"] == 1
    assert summary["errors"] == ["222: deleted"]

    record = yaml.safe_load((subs / "issue-1.yml").read_text())
    assert record["actual"]["favorites"] == 60
    assert record["actual"]["replies"] == 7
    assert record["actual"]["reposts"] == 9
    assert record["actual"]["quotes"] == 1
    assert record["actual"]["bookmarks"] == 5
    assert record["actual"]["impressions"] == 2500
    assert record["actual"]["source"] == "x-api-v2"

    snaps = yaml.safe_load((metrics / "issue-1.yml").read_text())["snapshots"]
    assert snaps[0]["like_count"] == 60
    assert snaps[0]["age_hours"] == 24.0
    err_snaps = yaml.safe_load((metrics / "issue-2.yml").read_text())["snapshots"]
    assert err_snaps[0]["status"] == "error"


def test_recent_snapshot_suppresses_refetch(tmp_path):
    subs, metrics = tmp_path / "subs", tmp_path / "metrics"
    write_submission(subs, "issue-1", url="https://x.com/f/status/111")
    metrics.mkdir(parents=True)
    (metrics / "issue-1.yml").write_text(yaml.safe_dump({
        "submission": "issue-1", "tweet_id": "111",
        "snapshots": [{"fetched_at": "2026-11-16T02:00:00Z", "status": "ok"}],
    }))
    # last snapshot is 10h old < 20h gap
    assert collect_targets(NOW, subs, metrics, 30, 20, force=False) == []
    assert len(collect_targets(NOW, subs, metrics, 30, 20, force=True)) == 1


def test_old_submissions_not_fetched(tmp_path):
    subs = tmp_path / "subs"
    write_submission(subs, "ancient", url="https://x.com/f/status/1",
                     submitted_at="2026-01-01T00:00:00Z")
    assert collect_targets(NOW, subs, tmp_path / "m", 30, 20, force=False) == []


def test_batching_respects_limit(tmp_path, monkeypatch):
    subs = tmp_path / "subs"
    for i in range(5):
        write_submission(subs, f"s{i}", url=f"https://x.com/f/status/{1000 + i}")
    monkeypatch.setattr(fetch_actuals, "BATCH_SIZE", 2)
    calls = []

    def fake_get(url, token):
        ids = url.split("ids=")[1].split("&")[0].split("%2C")
        calls.append(ids)
        return api_payload(*[
            {"id": i, "public_metrics": {"like_count": 1}} for i in ids
        ])

    summary = run(token="tok", submissions_dir=subs, metrics_dir=tmp_path / "m",
                  max_age_days=30, min_gap_hours=20, force=False,
                  http_get=fake_get, sleep=lambda s: None, now=NOW)
    assert [len(c) for c in calls] == [2, 2, 1]
    assert summary["fetched"] == 5


def test_rate_limit_waits_once_then_succeeds():
    responses = [
        (429, {"retry-after": "7"}, b""),
        api_payload({"id": "1", "public_metrics": {"like_count": 2}}),
    ]
    waits = []
    payload = fetch_batch(["1"], "tok", lambda u, t: responses.pop(0),
                          sleep=waits.append)
    assert waits == [7.0]
    assert payload["data"][0]["public_metrics"]["like_count"] == 2


def test_rate_limit_twice_gives_up():
    with pytest.raises(RuntimeError, match="still rate limited"):
        fetch_batch(["1"], "tok", lambda u, t: (429, {}, b""), sleep=lambda s: None)


def test_api_error_does_not_leak_token():
    with pytest.raises(RuntimeError) as exc:
        fetch_batch(["1"], "secret-token", lambda u, t: (403, {}, b"forbidden"),
                    sleep=lambda s: None)
    assert "secret-token" not in str(exc.value)
    assert "HTTP 403" in str(exc.value)
