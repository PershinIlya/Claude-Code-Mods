import sys

if len(sys.argv) > 1 and sys.argv[1] == "statusline":
    from .statusline import main

    sys.exit(main(sys.argv[2:]))

from .cli import main

sys.exit(main(sys.argv[1:]))
