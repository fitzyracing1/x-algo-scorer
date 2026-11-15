# x-algorithm scoring bot

A small, honest toy for poking at the public weights and feature switches from
[xai-org/x-algorithm](https://github.com/xai-org/x-algorithm): a YAML mirror of
the current values with source citations, a scorer that shows how each weight
contributes to a post's ranking score, and workflows that keep the mirror in
sync with upstream and let you score posts from GitHub Actions.

## This is not a predictor. Read this first.

The real ranker computes, **per viewer**, model-predicted probabilities —
P(this specific user favorites the post), P(they reply), expected dwell
seconds, and so on — and *those* are what the public weights multiply
(see `home-mixer/scorers/value_model.rs` and `xai-value-model/scoring.rs`
upstream). Nobody outside X has those models, those features, or that
per-viewer context.

This repo replicates the weighted-sum formula faithfully, but feeds it **your
guesses** (or made-up defaults). The resulting number is rough at best. It is
useful for exactly one thing: seeing how the weights and switches interact —
e.g. that a reply is worth ~7x a favorite per unit of probability, that a
report prediction is catastrophic, or that a switch everyone is tweeting about
currently does nothing. It will not tell you how your post will rank.

## What's here

| Path | What it is |
|---|---|
| `config/x-algorithm.yml` | Mirrored weights/switches, each citing its upstream param key, file, and the source commit |
| `xscore/` | The scorer (mirrors `xai-value-model/scoring.rs`: weighted sum, mutual-follow boosts, eligibility gates, `offset_score`) |
| `scripts/sync_upstream.py` | Checks upstream `main` for changes to tracked params; `--write` updates the YAML in place |
| `scripts/score_submission.py` | Score a post and optionally store it under `data/submissions/` |
| `scripts/calibration_report.py` | Compares your guessed probabilities with the real engagement your posts got |
| `.github/workflows/ci.yml` | Tests + end-to-end scoring smoke test |
| `.github/workflows/sync-upstream.yml` | Daily upstream check; opens a PR when tracked values change |
| `.github/workflows/score-post.yml` | `workflow_dispatch`: submit a post, get the breakdown in the job summary |

## Quick start

```bash
pip install -r requirements.txt
python -m xscore.cli examples/sample_post.yml   # score the example
python -m xscore.cli --text "gm"                # all-default guesses
pytest                                          # run the tests
python scripts/sync_upstream.py                 # check upstream for drift
```

Post input is YAML or JSON:

```yaml
text: "Just put my lap times from the sim into a spreadsheet..."
predictions:        # your guesses; anything omitted uses a labeled default
  favorite: 0.03
  reply: 0.004
  report: 0.00002
flags:
  in_network: true
  is_mutual_follow_author: false   # true adds the +15.0 reply weight boost
  vqv_eligible: false
  quoted_vqv_eligible: false
```

## Example output (abridged)

```text
Toy ranking score
-----------------
Post: "Just put my lap times from the sim into a spreadsheet and I am slower than I ..."
Weights from https://github.com/xai-org/x-algorithm @ 30647a8 (2026-11-14).

CAVEAT: upstream multiplies these weights by per-viewer model predictions
nobody outside X has. The probabilities here are your guesses plus made-up
defaults, so this score is rough at best.

Contribution breakdown
----------------------
  cont_dwell_time          5 expected dwell seconds  x  0.0075  = +0.037500
  not_interested                   0.0005  x   -53.07  = -0.026535 (default guess)
  favorite                           0.03  x    0.708  = +0.021240
  reply                             0.004  x        5  = +0.020000
  dwell                              0.35  x     0.05  = +0.017500
  ...
  report                            2e-05  x     -234  = -0.004680

Weighted sum (pre-offset): 0.071041
Final score (after offset_score): 0.072041

Switch analysis (post-scoring diversity stage)
----------------------------------------------
* DppEnabled=true: after scoring, the slate is re-ranked for diversity by a
  DPP (theta=0.65, applied to the top 150 candidates). ...
* DppWindow=0: sliding-window DPP is off. NO EFFECT: VMRankerSendDppHistory=true
  makes home-mixer send up to 50 recently-served posts as DPP history, but
  vm-ranker only reads that history inside the sliding-window path
  (vm-ranker/dpp.rs), which DppWindow=0 disables. The history is sent and
  then ignored.
```

## Where every value comes from

Every entry in `config/x-algorithm.yml` carries its upstream feature-switch
key (`param`) and file, and the top-level `source.commit` pins the exact
upstream commit the values were read from (currently
`30647a85a20cbdd114169beb9973680c4dc4f401`, 2026-11-14). Highlights, all from
that commit:

- **Engagement weights** — `home-mixer/params/param.rs`: reply `5.0`,
  favorite `0.708`, repost `1.0`, share `2.118`, quote `5.0`,
  follow-author `4.0`; negatives: report `-234.0`, mute `-58.8`,
  not-interested `-53.07`, block `-31.2`, not-dwelled `-0.02`. The upstream
  file itself warns these multiply predicted probabilities, not counts.
- **Mutual-follow boosts** — same file: reply `+15.0`, dwell `+0.0`.
- **DPP switches** — `vm-ranker/params.rs`: `DppEnabled: true` (true since it
  was introduced in commit `1b3fec2`, 2026-09-23), `TopicDppEnabled: false`,
  `DppTheta: 0.65`, `DppMaxSelectedRank: 150`, `DppWindow: 0`.
- **DPP history** — `home-mixer/params/param.rs`: `VMRankerSendDppHistory`
  flipped `false → true` and `VMRankerDppHistoryMaxItems` changed `200 → 50`
  in commit `30647a8` (2026-11-14; introduced in `590f392` the day before).
  Because `DppWindow` is still `0`, that history is currently sent and ignored
  (`vm-ranker/scoring/mod.rs` maps window `0` to `None`; `vm-ranker/dpp.rs`
  only reads history in the windowed path). The scorer's switch analysis
  points this kind of thing out automatically.

## Scoring via GitHub Actions

Run the **Score a post** workflow (`workflow_dispatch`) with your text and
optional probability guesses; the breakdown lands in the job summary. Check
**save** to store the submission as `data/submissions/<id>.yml`.

### Closing the loop: calibration

Stored submissions include an empty `actual:` block. After your post has been
up for a while, fill in what it really got:

```yaml
actual:
  impressions: 2500
  favorites: 60
  replies: 7
  reposts: 9
  quotes: 1
```

Then `python scripts/calibration_report.py` compares your guessed
probabilities against the observed per-impression rates and tells you how far
off your guesses run, head by head. (Per-impression rates are themselves only
a proxy for the per-viewer probabilities the real models predict — the report
says so, because that is the honest caveat.)

## Staying in sync with upstream

`sync-upstream.yml` runs daily, re-reads the tracked params from upstream
`main`, and opens a PR updating `config/x-algorithm.yml` when anything
changed. The pinned-value test in `tests/test_config.py` intentionally fails
on such PRs until a human re-verifies the new values — no silent weight
changes.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Joshua Almeida
(fitzyracing1). Not affiliated with X or xAI; upstream values belong to
[xai-org/x-algorithm](https://github.com/xai-org/x-algorithm).
