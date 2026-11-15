"""Sync-script parsing and YAML rewriting, exercised offline on fixtures."""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from sync_upstream import parse_params, rewrite_source, rewrite_value

RUST_FIXTURE = """
use xai_feature_switches::param;

param!(FavoriteWeight, f64, "rust_home_mixer_favorite_weight", 0.708);
param!(ReplyWeight, f64, "rust_home_mixer_reply_weight", 5.0);
param!(
    DppEnabled,
    bool,
    "rust_home_mixer_vm_ranker_dpp_enabled",
    true
);
param!(
    DppMaxSelectedRank,
    u32,
    "rust_home_mixer_vm_ranker_dpp_max_selected_rank",
    150
);
param!(ReportWeight, f64, "rust_home_mixer_report_weight", -234.0);
param!(SomeString, String, "rust_home_mixer_some_string", "nope");
pub const NEGATIVE_SCORES_OFFSET: f64 = 0.001;
"""


def test_parse_params_handles_inline_and_multiline():
    params = parse_params(RUST_FIXTURE)
    assert params["rust_home_mixer_favorite_weight"] == 0.708
    assert params["rust_home_mixer_reply_weight"] == 5.0
    assert params["rust_home_mixer_vm_ranker_dpp_enabled"] is True
    assert params["rust_home_mixer_vm_ranker_dpp_max_selected_rank"] == 150
    assert params["rust_home_mixer_report_weight"] == -234.0
    assert params["NEGATIVE_SCORES_OFFSET"] == 0.001
    assert "rust_home_mixer_some_string" not in params  # non-scalar defaults skipped


def test_parse_real_config_roundtrip():
    """Every param tracked in our config parses out of the real upstream syntax."""
    config = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config" / "x-algorithm.yml").read_text()
    )
    tracked = [
        entry["param"]
        for section in ("weights", "boosts", "constants", "switches")
        for entry in config[section].values()
    ]
    assert len(tracked) == len(set(tracked)), "duplicate param keys in config"


def test_rewrite_value_preserves_surroundings():
    text = (
        "weights:\n"
        "  favorite:\n"
        "    value: 0.708\n"
        "    param: rust_home_mixer_favorite_weight\n"
        "    file: home-mixer/params/param.rs\n"
        "  reply:\n"
        "    value: 5.0\n"
        "    param: rust_home_mixer_reply_weight\n"
    )
    out = rewrite_value(text, "rust_home_mixer_favorite_weight", 0.9)
    assert "value: 0.9" in out
    assert "value: 5.0" in out  # untouched sibling
    assert "file: home-mixer/params/param.rs" in out
    parsed = yaml.safe_load(out)
    assert parsed["weights"]["favorite"]["value"] == 0.9


def test_rewrite_value_bool_and_missing():
    text = "    value: true\n    param: rust_x\n"
    assert "value: false" in rewrite_value("\n" + text, "rust_x", False)
    with pytest.raises(RuntimeError):
        rewrite_value(text, "rust_missing", 1)


def test_rewrite_source_updates_commit():
    text = (
        "source:\n"
        "  repo: https://github.com/xai-org/x-algorithm\n"
        "  commit: aaaa\n"
        '  commit_date: "2026-01-01"\n'
        '  synced_at: "2026-01-01"\n'
    )
    out = rewrite_source(text, "b" * 40, "2026-12-31")
    assert f"commit: {'b' * 40}" in out
    assert 'commit_date: "2026-12-31"' in out
    assert 'synced_at: "2026-01-01"' not in out
