#!/usr/bin/env python3
"""Latency unit helpers shared by dataset builders.

The micro traces store gem5 ticks. Training labels must be expressed in CPU
cycles so that they are comparable across uarch profiles.
"""
import json
import os

import numpy as np


DEFAULT_GEM5_TICKS_PER_SECOND = 1_000_000_000_000.0


def load_uarch_profile(path):
    if not path:
        raise ValueError("uarch_profile path is required")
    if not os.path.exists(path):
        raise FileNotFoundError(f"uarch_profile not found: {path}")
    with open(path) as fp:
        prof = json.load(fp)
    freq = ((prof.get("core") or {}).get("freq_ghz"))
    if freq is None:
        raise ValueError(f"uarch_profile.core.freq_ghz missing: {path}")
    freq = float(freq)
    if freq <= 0:
        raise ValueError(f"uarch_profile.core.freq_ghz must be > 0: {freq}")
    tps = float(prof.get("sim_ticks_per_second",
                         DEFAULT_GEM5_TICKS_PER_SECOND))
    if tps <= 0:
        raise ValueError(f"sim_ticks_per_second must be > 0: {tps}")
    prof["_latency_unit"] = {
        "source_unit": "gem5_tick",
        "target_unit": "cycle",
        "freq_ghz": freq,
        "sim_ticks_per_second": tps,
        "ticks_per_cycle": tps / (freq * 1.0e9),
    }
    return prof


def ticks_to_cycles(value, profile):
    meta = profile.get("_latency_unit") or {}
    ticks_per_cycle = float(meta["ticks_per_cycle"])
    return np.asarray(value, dtype=np.float64) / ticks_per_cycle


def latency_unit_metadata(profile):
    meta = dict(profile.get("_latency_unit") or {})
    if not meta:
        raise ValueError("profile was not loaded by load_uarch_profile()")
    return meta
