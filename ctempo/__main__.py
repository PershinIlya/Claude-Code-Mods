import os
import sys

if os.name == "nt":  # as in bin/ctempo: a TZ the C runtime misreads goes first
    from .clock import windows_tz

    windows_tz()

if len(sys.argv) > 1 and sys.argv[1] == "statusline":
    from .statusline import main

    sys.exit(main(sys.argv[2:]))

from .cli import main

sys.exit(main(sys.argv[1:]))
