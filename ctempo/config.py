"""Paths and configuration.

Everything lives under CTEMPO_HOME (default ~/.claude-tempo; `~` is expanded
and a relative value is taken from the home directory). The config file is
optional: missing keys fall back to DEFAULTS, unknown keys are kept so a
newer config does not break an older ctempo.
"""

import copy
import json
import math
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
    "mod": {"surfaces": ["desktop"], "refresh_s": 60},
    "retention_days": 365,
    "write_min_interval_s": 60,
    "db_timeout_ms": 250,
}


def home():
    value = os.environ.get("CTEMPO_HOME")
    if not value:
        return os.path.join(os.path.expanduser("~"), ".claude-tempo")
    # Claude Code copies settings.json `env` values as written: expand `~`
    # here, and take a relative path from the home directory, never from the
    # session's cwd (each project would get a database of its own).
    return os.path.join(os.path.expanduser("~"), os.path.expanduser(value))


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


def _compatible(default, value):
    """A user value replaces a default only if it has the same kind, so a
    typo in config.json falls back to the default instead of breaking."""
    if default is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    return False


def _merge(base, over):
    for k, v in over.items():
        if k not in base:
            base[k] = v  # unknown keys are kept for newer versions
        elif isinstance(base[k], dict):
            if isinstance(v, dict):
                _merge(base[k], v)
        elif _compatible(base[k], v):
            base[k] = v
    return base


# Allowed ranges; a value outside is clamped (a typo must not break the line).
RANGES = {
    ("rate_window_min",): (2, 120),
    ("rate_min_samples",): (2, 100),
    ("rate_min_span_min",): (0.5, 60),
    ("verdict", "speed_up"): (1.0, 10.0),
    ("verdict", "slow_down"): (0.05, 1.0),
    ("tail", "five_hour_min"): (0, 300),
    ("tail", "seven_day_hours"): (0, 168),
    ("min_free",): (0, 100),
    ("profile", "half_life_days"): (0.5, 365),
    ("profile", "floor"): (0, 1),
    ("profile", "prior_strength"): (0, 100),
    ("profile", "prior_strength_uniform"): (0, 100),
    ("profile", "bootstrap_days"): (1, 365),
    ("profile", "max_gap_hours"): (0.1, 48),
    ("profile", "active_hours_lookback"): (1, 168),
    ("reset", "drop_points"): (1, 100),
    ("reset", "drop_samples"): (1, 10),
    ("display", "max_width"): (20, 400),
    ("compose", "timeout_ms"): (0, 10000),
    ("mod", "refresh_s"): (15, 3600),
    ("retention_days",): (7, 3650),
    ("write_min_interval_s",): (0, 3600),
    ("db_timeout_ms",): (10, 5000),
}
CHOICES = {("display", "symbols"): ("unicode", "ascii"), ("display", "color"): ("auto", "never", "always")}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _override(ov):
    """The profile override with each field checked on its own (a bad field
    falls back to its default, the rest is kept); None if not a dict."""
    if not isinstance(ov, dict):
        return None
    days = ov.get("work_days")
    days = sorted({int(d) for d in days if _num(d) and int(d) == d and 0 <= d <= 6}) if isinstance(days, list) else []
    wake = ov.get("wake_hours")
    if isinstance(wake, list) and len(wake) == 2 and all(_num(h) for h in wake):
        wake = [min(24, max(0, int(h))) for h in wake]
    else:
        wake = [8, 23]
    off = ov.get("off_day_factor")
    off = min(1.0, max(0.0, float(off))) if _num(off) else 0.5
    mode = str(ov.get("mode", "prior")).lower()
    return {"work_days": days or [0, 1, 2, 3, 4], "wake_hours": wake, "off_day_factor": off,
            "mode": mode if mode in ("prior", "replace") else "prior"}


def normalize(cfg):
    for keys, (lo, hi) in RANGES.items():
        d = cfg
        for k in keys[:-1]:
            d = d[k]
        d[keys[-1]] = min(hi, max(lo, d[keys[-1]]))
    for keys, allowed in CHOICES.items():
        d = cfg
        for k in keys[:-1]:
            d = d[k]
        if d[keys[-1]] not in allowed:
            d[keys[-1]] = DEFAULTS[keys[0]][keys[-1]]
    cmd = cfg["compose"].get("command")
    cfg["compose"]["command"] = cmd if isinstance(cmd, str) and cmd.strip() else None
    if not isinstance(cfg["compose"].get("separator"), str):
        cfg["compose"]["separator"] = "  "
    cfg["profile"]["override"] = _override(cfg["profile"].get("override"))
    given = cfg["mod"]["surfaces"]
    kept = [x for x in given if isinstance(x, str)]
    cfg["mod"]["surfaces"] = kept if (kept or not given) else ["desktop"]  # [] means: draw nowhere
    return cfg


def load(path=None):
    """Defaults merged with the user's config file, validated. Never raises."""
    try:
        cfg = copy.deepcopy(DEFAULTS)
        try:
            with open(path or config_path(), encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                _merge(cfg, user)
        except (OSError, ValueError):
            pass
        return normalize(cfg)
    except Exception:  # a pathological file (say, nested 200k deep): defaults
        return copy.deepcopy(DEFAULTS)


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
