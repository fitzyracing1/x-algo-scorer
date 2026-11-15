"""CLI: score a post file against the mirrored x-algorithm weights.

Usage:
  python -m xscore.cli examples/sample_post.yml
  python -m xscore.cli post.json --markdown
  python -m xscore.cli --text "hello world"
"""

from __future__ import annotations

import argparse
import sys

from xscore.scorer import (
    DEFAULT_CONFIG_PATH,
    format_breakdown,
    load_config,
    load_post,
    score_post,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="xscore",
        description="Toy scorer for the public x-algorithm weights. NOT a predictor.",
    )
    parser.add_argument("post", nargs="?", help="YAML/JSON post file (text, predictions, flags)")
    parser.add_argument("--text", help="score ad-hoc text with all-default guessed probabilities")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="config YAML path")
    parser.add_argument("--markdown", action="store_true", help="emit GitHub-flavored markdown")
    args = parser.parse_args(argv)

    if bool(args.post) == bool(args.text):
        parser.error("provide exactly one of: a post file, or --text")

    config = load_config(args.config)
    post = {"text": args.text} if args.text else load_post(args.post)
    result = score_post(post, config)
    print(format_breakdown(result, config, markdown=args.markdown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
