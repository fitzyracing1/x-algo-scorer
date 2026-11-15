"""Replicates the weighted-sum "value model" from xai-org/x-algorithm.

The math mirrors xai-value-model/scoring.rs at the commit recorded in
config/x-algorithm.yml:

  weighted = sum(prediction[head] * effective_weight[head])
  final    = offset_score(weighted)

where effective weights apply the mutual-follow reply/dwell boosts and the
eligibility gates (vqv, quoted_vqv, post_unexplored) exactly as upstream does.

What this file canNOT replicate is the predictions themselves: upstream feeds
per-viewer Phoenix model outputs (P(favorite), P(reply), expected dwell
seconds, ...). Here they are user guesses or made-up defaults, so the result
is a toy for exploring how the weights interact -- nothing more.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "x-algorithm.yml"

# Probability-like heads: value is a predicted probability in [0, 1].
PROBABILITY_HEADS = [
    "favorite",
    "reply",
    "retweet",
    "photo_expand",
    "video_open",
    "click",
    "open_link",
    "profile_click",
    "vqv",
    "share",
    "share_via_dm",
    "share_via_copy_link",
    "dwell",
    "quote",
    "quoted_click",
    "quoted_vqv",
    "follow_author",
    "post_unexplored",
    "post_video",
    "grok_analyze",
    "not_interested",
    "block_author",
    "mute_author",
    "report",
    "not_dwelled",
]

# Continuous heads: value is an expected quantity (seconds), not a probability.
CONTINUOUS_HEADS = {
    "cont_dwell_time": "expected dwell seconds",
    "cont_click_dwell_time": "expected post-click dwell seconds",
}

ALL_HEADS = PROBABILITY_HEADS + list(CONTINUOUS_HEADS)

# Made-up ballpark baseline guesses for an unremarkable text post shown to
# followers. These are NOT from X and NOT calibrated to anything; they exist
# so the toy produces a number without user input. Override freely.
DEFAULT_PREDICTIONS = {
    "favorite": 0.02,
    "reply": 0.002,
    "retweet": 0.004,
    "photo_expand": 0.0,
    "video_open": 0.0,
    "click": 0.01,
    "open_link": 0.0,
    "profile_click": 0.002,
    "vqv": 0.0,
    "share": 0.001,
    "share_via_dm": 0.0005,
    "share_via_copy_link": 0.0005,
    "dwell": 0.3,
    "quote": 0.0005,
    "quoted_click": 0.0,
    "quoted_vqv": 0.0,
    "follow_author": 0.0002,
    "post_unexplored": 0.0,
    "post_video": 0.0,
    "grok_analyze": 0.0005,
    "not_interested": 0.0005,
    "block_author": 0.0001,
    "mute_author": 0.0001,
    "report": 0.00005,
    "not_dwelled": 0.45,
    "cont_dwell_time": 4.0,
    "cont_click_dwell_time": 0.0,
}

DEFAULT_FLAGS = {
    "in_network": True,
    "is_mutual_follow_author": False,
    "vqv_eligible": False,
    "quoted_vqv_eligible": False,
}


@dataclass
class ScoreResult:
    text: str
    terms: dict[str, float]            # head -> contribution to weighted sum
    effective_weights: dict[str, float]
    predictions: dict[str, float]
    defaulted_heads: list[str]         # heads where DEFAULT_PREDICTIONS was used
    weighted_sum: float                # pre-offset sum of terms
    final_score: float                 # after offset_score()
    flags: dict[str, bool]
    switch_notes: list[str] = field(default_factory=list)
    inert_heads: list[str] = field(default_factory=list)  # weight == 0


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict:
    with open(path) as f:
        config = yaml.safe_load(f)
    for section in ("source", "weights", "boosts", "constants", "switches"):
        if section not in config:
            raise ValueError(f"config missing required section: {section}")
    missing = [h for h in ALL_HEADS if h not in config["weights"]]
    if missing:
        raise ValueError(f"config missing weights for heads: {missing}")
    return config


def load_post(path: str | Path) -> dict:
    """Load a post from YAML or JSON: {text, predictions?, flags?, actual?}."""
    path = Path(path)
    raw = path.read_text()
    post = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
    if not isinstance(post, dict) or "text" not in post:
        raise ValueError(f"{path}: post input must be a mapping with a 'text' key")
    unknown = set(post.get("predictions") or {}) - set(ALL_HEADS)
    if unknown:
        raise ValueError(f"{path}: unknown prediction heads: {sorted(unknown)}")
    return post


def _weight(config: dict, section: str, key: str) -> float:
    return float(config[section][key]["value"])


def effective_weights(config: dict, flags: dict) -> dict[str, float]:
    """Mirror the weight gating in xai-value-model (weights.rs + scoring.rs)."""
    w = {h: _weight(config, "weights", h) for h in ALL_HEADS}
    reply_boost = _weight(config, "boosts", "bidirectional_follow_reply_weight_boost")
    dwell_boost = _weight(config, "boosts", "bidirectional_follow_dwell_weight_boost")
    if flags["is_mutual_follow_author"]:
        if reply_boost != 0.0:
            w["reply"] += reply_boost
        if dwell_boost != 0.0:
            w["dwell"] += dwell_boost
    if not flags["vqv_eligible"]:
        w["vqv"] = 0.0
    if not flags["quoted_vqv_eligible"]:
        w["quoted_vqv"] = 0.0
    if not flags["in_network"]:
        # post_unexplored_include_out_of_network is hardcoded false upstream
        # (home-mixer/scorers/value_model.rs).
        w["post_unexplored"] = 0.0
    return w


def _negative_sum(config: dict) -> float:
    # Mirrors ValueModelWeights::negative_sum(): the five explicit negative heads.
    heads = ["not_interested", "block_author", "mute_author", "report", "not_dwelled"]
    return -sum(_weight(config, "weights", h) for h in heads)


def _total_sum(config: dict) -> float:
    # Mirrors positive_sum() + negative_sum(). positive_sum() upstream includes
    # every non-negative head weight (base weights, no boosts).
    positive_heads = [h for h in ALL_HEADS if h not in
                      ("not_interested", "block_author", "mute_author", "report", "not_dwelled")]
    positive = sum(_weight(config, "weights", h) for h in positive_heads)
    return positive + _negative_sum(config)


def offset_score(weighted: float, config: dict) -> float:
    """Mirror offset_score() in xai-value-model/scoring.rs."""
    offset = _weight(config, "constants", "negative_scores_offset")
    total = _total_sum(config)
    if total == 0.0:
        return max(weighted, 0.0)
    if weighted < 0.0:
        return (weighted + _negative_sum(config)) / total * offset
    return weighted + offset


def switch_analysis(config: dict) -> list[str]:
    """Human-readable notes on what the current switch values actually do."""
    s = {k: v["value"] for k, v in config["switches"].items()}
    notes = []
    if s["dpp_enabled"]:
        notes.append(
            f"DppEnabled=true: after scoring, the slate is re-ranked for diversity by a "
            f"DPP (theta={s['dpp_theta']}, applied to the top "
            f"{s['dpp_max_selected_rank']} candidates). This reorders posts relative to "
            f"each other; it is not part of this single-post score."
        )
    else:
        notes.append("DppEnabled=false: no DPP diversity re-ranking.")
    if not s["topic_dpp_enabled"]:
        notes.append(
            "TopicDppEnabled=false: topic-timeline requests skip the DPP entirely "
            "(dpp_enabled() in vm-ranker/scoring/mod.rs requires it for topic requests)."
        )
    if s["dpp_window"] == 0:
        extra = ""
        if s["vm_ranker_send_dpp_history"]:
            extra = (
                f" NO EFFECT: VMRankerSendDppHistory=true makes home-mixer send up to "
                f"{s['vm_ranker_dpp_history_max_items']} recently-served posts as DPP "
                f"history, but vm-ranker only reads that history inside the "
                f"sliding-window path (vm-ranker/dpp.rs), which DppWindow=0 disables "
                f"(window=None in vm-ranker/scoring/mod.rs). The history is sent and "
                f"then ignored."
            )
        notes.append(f"DppWindow=0: sliding-window DPP is off.{extra}")
    elif s["vm_ranker_send_dpp_history"]:
        notes.append(
            f"DppWindow={s['dpp_window']}: sliding-window DPP is on and consumes the "
            f"served history (max {s['vm_ranker_dpp_history_max_items']} items)."
        )
    if not s["dpp_greedy_order"] and s["dpp_window"] == 0:
        notes.append(
            "DppGreedyOrder=false: with the window also off, DPP keeps score order for "
            "selected items rather than greedy pick order."
        )
    return notes


def score_post(post: dict, config: dict) -> ScoreResult:
    flags = {**DEFAULT_FLAGS, **(post.get("flags") or {})}
    unknown_flags = set(flags) - set(DEFAULT_FLAGS)
    if unknown_flags:
        raise ValueError(f"unknown flags: {sorted(unknown_flags)}")

    user_preds = post.get("predictions") or {}
    predictions = {}
    defaulted = []
    for head in ALL_HEADS:
        if head in user_preds:
            predictions[head] = float(user_preds[head])
        else:
            predictions[head] = DEFAULT_PREDICTIONS[head]
            defaulted.append(head)
    for head in PROBABILITY_HEADS:
        p = predictions[head]
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"prediction for '{head}' must be in [0, 1], got {p}")

    weights = effective_weights(config, flags)
    terms = {h: predictions[h] * weights[h] for h in ALL_HEADS}
    weighted_sum = sum(terms.values())
    final = offset_score(weighted_sum, config)
    inert = [h for h in ALL_HEADS if weights[h] == 0.0]

    return ScoreResult(
        text=post["text"],
        terms=terms,
        effective_weights=weights,
        predictions=predictions,
        defaulted_heads=defaulted,
        weighted_sum=weighted_sum,
        final_score=final,
        flags=flags,
        switch_notes=switch_analysis(config),
        inert_heads=inert,
    )


def format_breakdown(result: ScoreResult, config: dict, markdown: bool = False) -> str:
    """Render a readable breakdown (plain text or GitHub-flavored markdown)."""
    src = config["source"]
    lines = []

    def h(text):
        lines.append(f"## {text}" if markdown else text)
        if not markdown:
            lines.append("-" * len(text))

    preview = result.text if len(result.text) <= 80 else result.text[:77] + "..."
    h("Toy ranking score")
    lines.append(f'Post: "{preview}"')
    lines.append(
        f"Weights from {src['repo']} @ {src['commit'][:7]} ({src['commit_date']})."
    )
    lines.append("")
    lines.append(
        "CAVEAT: upstream multiplies these weights by per-viewer model predictions "
        "nobody outside X has. The probabilities here are "
        + ("your guesses plus made-up defaults"
           if result.defaulted_heads else "your guesses")
        + ", so this score is rough at best and comparable only to other scores "
        "from this same toy."
    )
    lines.append("")

    h("Contribution breakdown")
    rows = sorted(result.terms.items(), key=lambda kv: -abs(kv[1]))
    if markdown:
        lines.append("| head | prediction | effective weight | contribution | |")
        lines.append("|---|---:|---:|---:|---|")
    for head, term in rows:
        tag = " (default guess)" if head in result.defaulted_heads else ""
        unit = CONTINUOUS_HEADS.get(head)
        pred = result.predictions[head]
        pred_s = f"{pred:g}" + (f" {unit}" if unit else "")
        w = result.effective_weights[head]
        if markdown:
            lines.append(f"| {head} | {pred_s} | {w:g} | {term:+.6f} | {tag.strip()} |")
        else:
            lines.append(f"  {head:<24} {pred_s:>14}  x {w:>8g}  = {term:+.6f}{tag}")
    lines.append("")
    lines.append(f"Weighted sum (pre-offset): {result.weighted_sum:.6f}")
    lines.append(
        f"Final score (after offset_score): {result.final_score:.6f}"
    )
    lines.append("")

    if result.inert_heads:
        h("Heads with no effect right now (weight is 0)")
        lines.append(", ".join(sorted(result.inert_heads)))
        lines.append(
            "(video_open, vqv, profile_click etc. have 0.0 defaults upstream; "
            "vqv/quoted_vqv are also zeroed here unless the eligibility flags are set.)"
        )
        lines.append("")

    h("Switch analysis (post-scoring diversity stage)")
    for note in result.switch_notes:
        lines.append(("- " if markdown else "* ") + note)
    return "\n".join(lines)
