"""Behaviour a printer does not report about itself, per model.

Everything a printer can tell us (model, image size, size limit, frame size)
is read from it on connect. Only timing and orientation rules live here, and
each stays unverified until a real print confirms it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProfile:
    model: str
    max_jpeg_bytes: int  # working cap; the printer's own reported limit wins when it is smaller
    packet_gap_s: float  # pause after each frame is sent
    pre_print_delay_s: float  # minimum time between UPLOAD_END and PRINT
    flip_vertical: bool
    upload_window: int = 1  # frames sent before waiting for the first acknowledgement
    verified: bool = False  # True once a real print has confirmed the values above


GENERIC_MINI = ModelProfile(
    model="mini",
    max_jpeg_bytes=105 * 1024,
    packet_gap_s=0.05,
    pre_print_delay_s=1.0,
    flip_vertical=False,
)

# mini Link 3 (FI033), firmware 0102, measured over the Classic link on 2026-10-20:
# no gap and 4 frames in flight upload 54 KB in 0.82 s (1 in flight: 1.69 s;
# 8: 0.80 s; 16 overran the printer). Verified by a real print the same day
# (16.7 KB JPEG): this timing, the 1 s pause before PRINT, and NO flip, since the
# image printed the right way up. The community "vertical flip" came from an iOS
# app whose drawing context draws images upside down. Not yet printed: images
# between 16.7 KB and the 55 KB cap, and a pause shorter than 1 s.
MINI_LINK_3 = ModelProfile(
    model="FI033",
    max_jpeg_bytes=55_000,
    packet_gap_s=0.0,
    pre_print_delay_s=1.0,
    flip_vertical=False,
    upload_window=4,
    verified=True,
)
# The community timing, kept as a fallback if a printer misbehaves with the fast one.
MINI_LINK_3_SAFE = ModelProfile(
    model="FI033",
    max_jpeg_bytes=55_000,
    packet_gap_s=0.075,
    pre_print_delay_s=1.0,
    flip_vertical=False,
    upload_window=1,
)

PROFILES = {MINI_LINK_3.model: MINI_LINK_3}


def profile_for(model: str | None) -> ModelProfile:
    return PROFILES.get((model or "").strip().upper(), GENERIC_MINI)
