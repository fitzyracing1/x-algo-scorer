"""Issue-form parsing, actuals parsing, and the full event round trip."""

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import issue_bot
from issue_bot import handle_comment, handle_opened, parse_actuals, parse_issue_form

FORM_BODY = """### Post URL

https://x.com/fitzyracing1/status/12345

### Post text

Just put my lap times into a spreadsheet. Full data in the reply.

### Guessed probabilities (JSON, optional)

```json
{"favorite": 0.03, "reply": 0.004}
```
"""

FORM_BODY_MINIMAL = """### Post URL

_No response_

### Post text

gm

### Guessed probabilities (JSON, optional)

_No response_
"""


def test_parse_issue_form_full():
    form = parse_issue_form(FORM_BODY)
    assert form["url"] == "https://x.com/fitzyracing1/status/12345"
    assert form["text"].startswith("Just put my lap times")
    assert form["probabilities"] == {"favorite": 0.03, "reply": 0.004}


def test_parse_issue_form_minimal():
    form = parse_issue_form(FORM_BODY_MINIMAL)
    assert form == {"text": "gm"}


def test_parse_issue_form_bad_json():
    body = FORM_BODY.replace('{"favorite": 0.03, "reply": 0.004}', "{not json")
    with pytest.raises(ValueError, match="probabilities JSON"):
        parse_issue_form(body)


def test_parse_actuals_variants():
    assert parse_actuals("likes: 120, replies: 6, reposts: 14") == {
        "favorites": 120, "replies": 6, "reposts": 14,
    }
    assert parse_actuals("Views: 40,000 and quotes=3") == {
        "impressions": 40000, "quotes": 3,
    }
    assert parse_actuals("great post, congrats!") == {}
    assert parse_actuals("retweets: 9") == {"reposts": 9}


def test_opened_scores_and_stores(tmp_path):
    issue = {"number": 7, "body": FORM_BODY}
    comment = handle_opened(issue, tmp_path)
    assert "rough at best" in comment
    assert "| head |" in comment  # markdown breakdown table
    record = yaml.safe_load((tmp_path / "issue-7.yml").read_text())
    assert record["issue"] == 7
    assert record["predictions"] == {"favorite": 0.03, "reply": 0.004}
    assert record["url"].endswith("/12345")
    assert record["score"]["final_score"] > 0


def test_opened_without_text(tmp_path):
    comment = handle_opened({"number": 8, "body": "### Post text\n\n_No response_"}, tmp_path)
    assert "could not find any" in comment.lower()
    assert not (tmp_path / "issue-8.yml").exists()


def test_comment_stores_actuals_and_compares(tmp_path):
    issue = {"number": 7, "body": FORM_BODY}
    handle_opened(issue, tmp_path)
    reply = handle_comment(
        issue,
        {"body": "likes: 60, replies: 7, reposts: 9, quotes: 1, impressions: 2500"},
        tmp_path,
    )
    record = yaml.safe_load((tmp_path / "issue-7.yml").read_text())
    assert record["actual"]["favorites"] == 60
    assert record["actual"]["impressions"] == 2500
    assert "guess / observed" in reply
    assert "1.25x" in reply  # guessed 0.03 vs 60/2500 = 0.024


def test_comment_without_impressions_asks_for_them(tmp_path):
    issue = {"number": 9, "body": FORM_BODY}
    handle_opened(issue, tmp_path)
    reply = handle_comment(issue, {"body": "likes: 10, replies: 2"}, tmp_path)
    assert "impressions" in reply
    record = yaml.safe_load((tmp_path / "issue-9.yml").read_text())
    assert record["actual"]["favorites"] == 10


def test_comment_ignores_chatter(tmp_path):
    assert handle_comment({"number": 1, "body": ""}, {"body": "nice"}, tmp_path) is None


def test_comment_with_no_stored_submission(tmp_path):
    reply = handle_comment({"number": 99, "body": ""}, {"body": "likes: 5"}, tmp_path)
    assert "no stored submission" in reply


def test_main_end_to_end(tmp_path, monkeypatch):
    event = {"issue": {"number": 11, "body": FORM_BODY}}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    out = tmp_path / "comment.md"
    gh_out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    assert issue_bot.main(
        ["--event", str(event_path), "--out", str(out), "--data-dir", str(tmp_path / "subs")]
    ) == 0
    assert "Final score" in out.read_text() or "final" in out.read_text().lower()
    assert "has_comment=true" in gh_out.read_text()
