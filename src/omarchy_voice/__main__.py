"""`python3 -m omarchy_voice` — the entry point the omarchy-* wrappers use.

They cannot call the `omarchy-voice` binary by name: the wrapper for the
`omarchy voice` route is itself named `omarchy-voice`, so a PATH lookup finds
the wrapper and execs it again. Going through the module names the
implementation unambiguously.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
