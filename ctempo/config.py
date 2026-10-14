"""Paths and configuration.

Everything lives under CTEMPO_HOME (default ~/.claude-tempo). The config file
is optional: missing keys fall back to DEFAULTS, unknown keys are kept so a
newer config does not break an older ctempo.
"""

import copy
import json
import os

FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"
WINDOWS = (FIVE_HOUR, SEVEN_DAY)
DURATION = {FIVE_HOUR: 5 * 3600, SEVEN_DAY: 7 * 86400}
LABEL = {FIVE_HOUR: "5h", SEVEN_DAY: "wk"}

DEFAULTS = {
    # Level each window is paced against, in percent of the limit. 100 means
    # "use it all by the reset"; 90 keeps a 10 point reserve.
    "target_5h": 100.0,
    "target_week": 100.0,
    # 5-hour burn rate: least squares over the trailing window.
    "rate_window_min": 20,
    "rate_min_samples": 3,
    "rate_min_span_min": 5,
    # Multiplier thresholds for the verdict: >= speed_up means you can go
    # faster, < slow_down means you will overshoot the target.
    "verdict": {"speed_up": 1.2, "slow_down": 0.9},
    # A window is "in its tail" this close to its reset (used by colors and
    # by `ctempo check`).
    "tail": {"five_hour_min": 60, "seven_day_hours": 48},
    # Points of projected surplus that count as worth harvesting.
    "min_free": 10.0,
    "profile": {
        "half_life_days": 14.0,
        # Off-hours keep this fraction of the mean awake-hour weight, so the
        # expected pace never drops to zero (ccpool uses the same 0.15).
        "floor": 0.15,
        # Prior strength in observation-equivalents (see DECISIONS.md D-12);
        # a flat prior (no transcripts, no override) gets the weaker one.
        "prior_strength": 1.0,
        "prior_strength_uniform": 0.25,
        "bootstrap_days": 45,
        # Gaps between fresh readings longer than this carry little shape
        # information and are learned with low confidence.
        "max_gap_hours": 2.0,
        "active_hours_lookback": 24,
        # Manual override, like ccpool's work days and wake hours. Example:
        # {"work_days": [0,1,2,3,4], "wake_hours": [8, 23],
        #  "off_day_factor": 0.3, "mode": "prior"}  (mode "prior" or "replace")
        "override": None,
    },
    "reset": {
        # A drop counts as a reset only if this many consecutive fresh
        # readings are at least drop_points below the window's max (ccpace).
        "drop_points": 15.0,
        "drop_samples": 2,
    },
    "display": {
        "symbols": "unicode",  # or "ascii"
        "max_width": 80,
        "color": "auto",  # "auto" honors NO_COLOR, "never", "always"
    },
    # Composition with a pre-existing statusLine command (set by install).
    "compose": {
        "command": None,
        "timeout_ms": 1500,
        "separator": "  ",
    },
    # Surfaces where the mod draws its band. The terminal already has the
    # statusLine, so it is left out by default.
    "mod": {"surfaces": ["desktop", "vscode", "mobile"], "refresh_s": 60},
    "retention_days": 365,
    "write_min_interval_s": 60,
    "db_timeout_ms": 250,
}


def home():
    return os.environ.get("CTEMPO_HOME") or os.path.join(os.path.expanduser("~"), ".claude-tempo")


def db_path():
    return os.path.join(home(), "tempo.db")


def config_path():
    return os.path.join(home(), "config.json")


def runtime_path():
    return os.path.join(home(), "runtime.json")


def log_path():
    return os.path.join(home(), "errors.log")


def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path=None):
    """Defaults merged with the user's config file. Never raises."""
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(path or config_path(), encoding="utf-8") as f:
            user = json.load(f)
        if isinstance(user, dict):
            _merge(cfg, user)
    except (OSError, ValueError):
        pass
    return cfg


def target(cfg, window):
    t = cfg["target_5h"] if window == FIVE_HOUR else cfg["target_week"]
    try:
        t = float(t)
    except (TypeError, ValueError):
        t = 100.0
    return min(100.0, max(1.0, t))


def write_defaults(path=None):
    """Write the default config if none exists. Returns the path."""
    path = path or config_path()
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS, f, indent=2)
            f.write("\n")
    return path
