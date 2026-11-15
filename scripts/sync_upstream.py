"""Check xai-org/x-algorithm for changes to the mirrored params.

Fetches the upstream param files at the tip of main, parses every
`param!(Name, type, "key", default)` plus NEGATIVE_SCORES_OFFSET, and compares
against config/x-algorithm.yml by feature-switch key.

With --write it edits the YAML in place (targeted text replacement, so the
hand-written comments and citations survive) and refreshes the source commit.
Without --write it only reports.

Exit code is always 0 on success; machine-readable outcome goes to
$GITHUB_OUTPUT (changed=true/false) when running in Actions.

No dependencies beyond PyYAML and the standard library; uses the public
GitHub API anonymously.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

import yaml

UPSTREAM = "xai-org/x-algorithm"
PARAM_FILES = [
    "home-mixer/params/param.rs",
    "vm-ranker/params.rs",
    "xai-value-model/weights.rs",
]
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "x-algorithm.yml"

PARAM_RE = re.compile(
    r'param!\(\s*(?P<name>\w+)\s*,\s*(?P<type>[\w:<>]+)\s*,\s*'
    r'"(?P<key>[^"]+)"\s*,\s*(?P<value>[^)]+?)\s*\)\s*;',
    re.DOTALL,
)
CONST_RE = re.compile(
    r'pub\s+const\s+(?P<name>\w+)\s*:\s*f64\s*=\s*(?P<value>[-\d.eE_]+)\s*;'
)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "x-algorithm-scoring-bot"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def upstream_head() -> tuple[str, str]:
    """Return (commit_sha, commit_date) of upstream main."""
    data = json.loads(fetch(f"https://api.github.com/repos/{UPSTREAM}/commits/main"))
    sha = data["sha"]
    date = data["commit"]["committer"]["date"][:10]
    return sha, date


def parse_value(raw: str):
    raw = raw.strip()
    if raw in ("true", "false"):
        return raw == "true"
    num = float(raw)
    return int(num) if ("." not in raw and "e" not in raw.lower()) else num


def parse_params(source: str) -> dict:
    """Map feature-switch key -> default value from a Rust params file."""
    out = {}
    for m in PARAM_RE.finditer(source):
        try:
            out[m.group("key")] = parse_value(m.group("value"))
        except ValueError:
            pass  # non-scalar default (string/enum); not tracked by this config
    for m in CONST_RE.finditer(source):
        out[m.group("name")] = parse_value(m.group("value"))
    return out


def fetch_upstream_params(sha: str) -> dict[str, dict]:
    """file path -> {feature-switch key -> value} at the given commit."""
    result = {}
    for path in PARAM_FILES:
        url = f"https://raw.githubusercontent.com/{UPSTREAM}/{sha}/{path}"
        result[path] = parse_params(fetch(url).decode())
    return result


def tracked_entries(config: dict):
    """Yield (section, name, entry) for every mirrored value."""
    for section in ("weights", "boosts", "constants", "switches"):
        for name, entry in config[section].items():
            yield section, name, entry


def yaml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value) if isinstance(value, float) else str(value)


def rewrite_value(text: str, param_key: str, new_value) -> str:
    """Replace the `value:` line that precedes `param: <key>` in our YAML."""
    pattern = re.compile(
        r"(value: )([^\n]+)(\n\s*param: " + re.escape(param_key) + r"\s*\n)"
    )
    if not pattern.search(text):
        raise RuntimeError(f"could not locate YAML entry for param {param_key}")
    return pattern.sub(lambda m: m.group(1) + yaml_scalar(new_value) + m.group(3), text, count=1)


def rewrite_source(text: str, sha: str, date: str) -> str:
    today = datetime.date.today().isoformat()
    text = re.sub(r"(\n  commit: )\S+", rf"\g<1>{sha}", text, count=1)
    text = re.sub(r'(\n  commit_date: )"[^"]*"', rf'\g<1>"{date}"', text, count=1)
    text = re.sub(r'(\n  synced_at: )"[^"]*"', rf'\g<1>"{today}"', text, count=1)
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="update the YAML in place")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    config = yaml.safe_load(config_path.read_text())
    old_sha = config["source"]["commit"]

    sha, date = upstream_head()
    print(f"upstream {UPSTREAM} main is at {sha[:12]} ({date}); config has {old_sha[:12]}")
    upstream = fetch_upstream_params(sha)

    changes, missing = [], []
    for section, name, entry in tracked_entries(config):
        key, path, old = entry["param"], entry["file"], entry["value"]
        new = upstream.get(path, {}).get(key)
        if new is None:
            missing.append(f"{key} (not found in {path})")
        elif new != old:
            changes.append((section, name, key, path, old, new))

    lines = []
    if changes:
        lines.append(f"Tracked values changed upstream ({old_sha[:7]} -> {sha[:7]}):")
        for section, name, key, path, old, new in changes:
            lines.append(f"  {section}.{name}: {old} -> {new}   [{key} in {path}]")
    else:
        lines.append("No tracked values changed upstream.")
    if missing:
        lines.append("WARNING: tracked params no longer found upstream (renamed/removed?):")
        lines.extend(f"  {m}" for m in missing)
    report = "\n".join(lines)
    print(report)

    if args.write and changes:
        text = config_path.read_text()
        for _, _, key, _, _, new in changes:
            text = rewrite_value(text, key, new)
        text = rewrite_source(text, sha, date)
        config_path.write_text(text)
        print(f"updated {config_path}")

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"changed={'true' if changes else 'false'}\n")
            f.write(f"upstream_sha={sha}\n")
            f.write("report<<EOF\n" + report + "\nEOF\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
