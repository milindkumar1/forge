"""Command wrapper for Forge's library-backed local inference sweep."""

import sys

from forge.local_benchmark import main

if __name__ == "__main__":
    sys.exit(main())
