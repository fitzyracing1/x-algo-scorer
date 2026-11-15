"""Scorer math must mirror xai-value-model/scoring.rs (verified at 30647a8)."""

import math

import pytest

from xscore.scorer import (
    DEFAULT_PREDICTIONS,
    format_breakdown,
    load_config,
    offset_score,
    score_post,
    switch_analysis,
)


@pytest.fixture(scope="module")
def config():
    return load_config()


def zeroed_predictions(**overrides):
    preds = {h: 0.0 for h in DEFAULT_PREDICTIONS}
    preds.update(overrides)
    return preds


def test_simple_weighted_sum(config):
    post = {
        "text": "hand-computed",
        "predictions": zeroed_predictions(favorite=0.1, reply=0.01, report=0.0001),
    }
    result = score_post(post, config)
    expected = 0.1 * 0.708 + 0.01 * 5.0 + 0.0001 * -234.0
    assert expected > 0
    assert math.isclose(result.weighted_sum, expected, rel_tol=1e-12)
    # positive sum -> final = sum + NEGATIVE_SCORES_OFFSET
    assert math.isclose(result.final_score, expected + 0.001, rel_tol=1e-12)


def test_negative_score_offset_branch(config):
    """Mirrors offset_score(): negative scores are squashed into (0, offset)."""
    post = {"text": "reported", "predictions": zeroed_predictions(report=0.05)}
    result = score_post(post, config)
    weighted = 0.05 * -234.0
    negative_sum = 53.07 + 31.2 + 58.8 + 234.0 + 0.02
    positive_sum = sum(
        config["weights"][h]["value"]
        for h in config["weights"]
        if h not in ("not_interested", "block_author", "mute_author", "report", "not_dwelled")
    )
    expected = (weighted + negative_sum) / (positive_sum + negative_sum) * 0.001
    assert math.isclose(result.final_score, expected, rel_tol=1e-12)
    assert 0.0 < result.final_score < 0.001


def test_offset_score_zero_total_degenerate():
    degenerate = {
        "weights": {h: {"value": 0.0} for h in DEFAULT_PREDICTIONS},
        "constants": {"negative_scores_offset": {"value": 0.001}},
    }
    assert offset_score(-5.0, degenerate) == 0.0
    assert offset_score(3.0, degenerate) == 3.0


def test_mutual_follow_reply_boost(config):
    preds = zeroed_predictions(reply=0.01)
    base = score_post({"text": "x", "predictions": preds}, config)
    boosted = score_post(
        {"text": "x", "predictions": preds, "flags": {"is_mutual_follow_author": True}},
        config,
    )
    assert math.isclose(base.weighted_sum, 0.01 * 5.0, rel_tol=1e-12)
    assert math.isclose(boosted.weighted_sum, 0.01 * (5.0 + 15.0), rel_tol=1e-12)
    # dwell boost is 0.0 upstream, so mutual follow must not change dwell weight
    assert boosted.effective_weights["dwell"] == base.effective_weights["dwell"]


def test_vqv_gated_by_eligibility(config):
    preds = zeroed_predictions(vqv=0.9)
    gated = score_post({"text": "x", "predictions": preds}, config)
    assert gated.weighted_sum == 0.0
    assert "vqv" in gated.inert_heads
    # even when eligible, the current upstream vqv weight is 0.0
    eligible = score_post(
        {"text": "x", "predictions": preds, "flags": {"vqv_eligible": True}}, config
    )
    assert eligible.effective_weights["vqv"] == 0.0


def test_post_unexplored_gated_by_in_network(config):
    preds = zeroed_predictions(post_unexplored=1.0)
    oon = score_post(
        {"text": "x", "predictions": preds, "flags": {"in_network": False}}, config
    )
    assert oon.weighted_sum == 0.0
    in_net = score_post({"text": "x", "predictions": preds}, config)
    assert math.isclose(in_net.weighted_sum, 0.015, rel_tol=1e-12)


def test_defaults_are_tracked_and_score_is_positive(config):
    result = score_post({"text": "all defaults"}, config)
    assert set(result.defaulted_heads) == set(DEFAULT_PREDICTIONS)
    assert result.final_score > 0


def test_probability_bounds_enforced(config):
    with pytest.raises(ValueError, match="must be in"):
        score_post({"text": "x", "predictions": {"favorite": 1.5}}, config)


def test_unknown_flag_rejected(config):
    with pytest.raises(ValueError, match="unknown flags"):
        score_post({"text": "x", "flags": {"verified_boost": True}}, config)


def test_switch_analysis_flags_inert_dpp_history(config):
    notes = " ".join(switch_analysis(config))
    assert "NO EFFECT" in notes
    assert "DppWindow=0" in notes
    assert "ignored" in notes


def test_breakdown_renders(config):
    result = score_post({"text": "breakdown please"}, config)
    text = format_breakdown(result, config)
    assert "rough at best" in text
    assert "favorite" in text and "report" in text
    md = format_breakdown(result, config, markdown=True)
    assert "| head |" in md
