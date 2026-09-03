#!/usr/bin/env python3
"""
client.py — thin launcher for the Bella desktop client.

Connects THIS machine to a Bella server as a device (no model runs here).

    python client.py --server ws://mac.local:8765 --token XXXX
    # or set BELLA_SERVER / BELLA_TOKEN and just:  python client.py

Only dependency: `websockets`  (pip install -r clients/requirements.txt)
Equivalent: `python -m clients.desktop.client` / `bella client`.
"""

import sys

from clients.desktop.client import main

if __name__ == "__main__":
    sys.exit(main())
