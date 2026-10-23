"""Installs made before pacer was called ctempo: what `pacer install --apply`
moves over, once. Until then they keep working: bin/ctempo still runs pacer,
CTEMPO_HOME is still read, and the old folder is used while the new one does
not exist (config.home).

  ~/.claude-tempo             -> ~/.pacer (moved, readings and all)
  the ctempo@claude-tempo mod -> uninstalled, its marketplace removed (the
                                 pacer mod is installed in its place)
  SwiftBar's ctempo.1m.sh     -> pacer.1m.sh
  the statusLine and backups  -> recognized as pacer's (install.is_ours,
                                 install.BACKUP_SUFFIX) and rewritten
"""

import os

from . import config

OLD_DIR = ".claude-tempo"
OLD_ENV = "CTEMPO_HOME"
OLD_PLUGIN_ID = "ctempo@claude-tempo"
OLD_MARKETPLACE = "claude-tempo"
OLD_MENUBAR = "ctempo.1m.sh"
OLD_MENUBAR_MARK = "# ctempo menu bar plugin"


def default_home(name):
    """The default folder `name` in the user's home, where this OS puts it."""
    if config.IS_WINDOWS:
        import ntpath

        return ntpath.join(ntpath.expanduser("~"), name)
    return os.path.join(os.path.expanduser("~"), name)


def move_home(out=print):
    """Move the old default folder to the new one when nothing names a
    folder (PACER_HOME, CTEMPO_HOME) and the new one does not exist yet."""
    if os.environ.get("PACER_HOME") or os.environ.get(OLD_ENV):
        return False
    old, new = default_home(OLD_DIR), default_home(".pacer")
    if os.path.isdir(old) and not os.path.exists(new) and os.path.normcase(old) != os.path.normcase(new):
        try:
            os.rename(old, new)
        except OSError as e:
            out("  could not move %s to %s: %s (pacer keeps using the old folder)" % (old, new, e))
            return False
        out("Moved %s to %s (pacer was called ctempo)" % (old, new))
        return True
    return False


def old_mod_listed(us):
    """Whether the user settings file `us` names the ctempo mod or its marketplace."""
    from . import install

    try:
        obj, _, _ = install.read_settings(us)
    except (OSError, ValueError, TypeError):
        return False
    ep, km = obj.get("enabledPlugins"), obj.get("extraKnownMarketplaces")
    return (isinstance(ep, dict) and OLD_PLUGIN_ID in ep) or (isinstance(km, dict) and OLD_MARKETPLACE in km)


def remove_old_mod(run, out=print):
    """Uninstall the ctempo mod and its marketplace.
    `run(args)` runs claude with args and returns (code, stdout, stderr)."""
    code, so, se = run(["plugin", "uninstall", OLD_PLUGIN_ID, "--scope", "user"])
    if code != 0:
        return False  # not installed (or claude says why): nothing of ours to remove
    out("  removed the ctempo mod (pacer was called ctempo)")
    run(["plugin", "marketplace", "remove", OLD_MARKETPLACE])
    return True


def move_menubar(out=print):
    """Replace SwiftBar's ctempo.1m.sh with pacer.1m.sh in the same folder."""
    from . import install, menubar

    state = install.load_state()
    path = state.get("menubar")
    if not (isinstance(path, str) and os.path.basename(path) == OLD_MENUBAR and os.path.exists(path)):
        return False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            if OLD_MENUBAR_MARK not in f.read(4096):
                return False
        os.remove(path)
    except OSError:
        return False
    out("  SwiftBar: %s replaced (pacer was called ctempo)" % OLD_MENUBAR)
    menubar.install(os.path.dirname(path))
    return True
