"""Frozen-app entry point for the DiscForge window (a file argument opens that game)."""
import sys

from discforge.gui.app import main

sys.exit(main(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None))
