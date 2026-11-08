# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point: python -I ot.py <command> ... (see python -I ot.py --help)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from opentool.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
