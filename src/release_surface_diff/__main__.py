"""Allow `python -m release_surface_diff ...`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
