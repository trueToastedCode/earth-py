#!/usr/bin/env python3
"""Earth Tile Library - Python port (download + 3D Tiles "small ZIP + script" export of a workflow).

    python3 earth_py.py WORKFLOW.workflow.json [PROXIES.txt] [--out DIR] [...]

See README.md, or run with --help.
"""

import sys

if sys.version_info < (3, 7):
    sys.exit("Python 3.7 or newer is needed.")

from earthpy.cli import main

if __name__ == "__main__":
    sys.exit(main())
