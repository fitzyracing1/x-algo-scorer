"""Toy scorer for the public xai-org/x-algorithm weights.

Not a predictor: the real ranker multiplies these weights by per-viewer
model predictions nobody outside X has.
"""

from xscore.scorer import (
    DEFAULT_PREDICTIONS,
    ScoreResult,
    load_config,
    load_post,
    score_post,
)

__all__ = [
    "DEFAULT_PREDICTIONS",
    "ScoreResult",
    "load_config",
    "load_post",
    "score_post",
]
