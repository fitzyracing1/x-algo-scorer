"""The config must stay structurally sound and match the verified upstream values."""

import pytest

from xscore.scorer import ALL_HEADS, load_config


@pytest.fixture(scope="module")
def config():
    return load_config()


def test_source_block(config):
    src = config["source"]
    assert src["repo"] == "https://github.com/xai-org/x-algorithm"
    assert len(src["commit"]) == 40, "source.commit must be a full SHA"
    assert src["commit_date"]


def test_every_head_has_a_weight_entry(config):
    for head in ALL_HEADS:
        entry = config["weights"][head]
        assert isinstance(entry["value"], (int, float))
        assert entry["param"].startswith("rust_home_mixer_")
        assert entry["file"] == "home-mixer/params/param.rs"


def test_every_entry_cites_param_and_file(config):
    for section in ("weights", "boosts", "constants", "switches"):
        for name, entry in config[section].items():
            assert "value" in entry, f"{section}.{name} missing value"
            assert entry.get("param"), f"{section}.{name} missing param citation"
            assert entry.get("file"), f"{section}.{name} missing file citation"


def test_headline_values_match_verified_upstream(config):
    """Values verified by hand against upstream commit 30647a8."""
    w = {k: v["value"] for k, v in config["weights"].items()}
    assert w["reply"] == 5.0
    assert w["favorite"] == 0.708
    assert w["report"] == -234.0
    assert w["retweet"] == 1.0
    assert w["share"] == 2.118
    assert w["not_interested"] == -53.07
    assert w["block_author"] == -31.2
    assert w["mute_author"] == -58.8

    s = {k: v["value"] for k, v in config["switches"].items()}
    assert s["dpp_enabled"] is True
    assert s["topic_dpp_enabled"] is False
    assert s["dpp_theta"] == 0.65
    assert s["dpp_max_selected_rank"] == 150
    assert s["dpp_window"] == 0
    assert s["vm_ranker_send_dpp_history"] is True
    assert s["vm_ranker_dpp_history_max_items"] == 50

    assert config["boosts"]["bidirectional_follow_reply_weight_boost"]["value"] == 15.0
    assert config["constants"]["negative_scores_offset"]["value"] == 0.001


def test_missing_section_rejected(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text("source: {}\nweights: {}\n")
    with pytest.raises(ValueError, match="missing required section"):
        load_config(bad)
