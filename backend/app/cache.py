"""In-memory store for a fetched analysis, so manual correction overrides
don't need to re-fetch satellite tiles or re-run SLIC on every edit -- only
the (cheap) painting + stats step reruns.

Single-process, in-memory, capped at a small count with oldest-first
eviction. That's the right amount of state for a local single-user dev
tool; it does mean entries are lost on backend restart/reload (the
frontend surfaces that as "please re-analyze").
"""

import secrets
from dataclasses import dataclass
from typing import Callable

import numpy as np
from PIL import Image

CACHE_MAX_ENTRIES = 30


@dataclass
class CachedAnalysis:
    image: Image.Image
    base_pixel_class: np.ndarray
    mask: np.ndarray
    to_px: Callable[[float, float], tuple[float, float]]
    bounds: tuple[float, float, float, float]  # south, west, north, east
    zoom: int
    meters_per_pixel: float
    created_at: float


_store: dict[str, CachedAnalysis] = {}


def put(entry: CachedAnalysis) -> str:
    if len(_store) >= CACHE_MAX_ENTRIES:
        oldest_id = min(_store, key=lambda k: _store[k].created_at)
        del _store[oldest_id]
    analysis_id = secrets.token_urlsafe(12)
    _store[analysis_id] = entry
    return analysis_id


def get(analysis_id: str) -> CachedAnalysis | None:
    return _store.get(analysis_id)
