"""
CardVault — a local MTG collection manager.

Features:
  - Live Scryfall lookup (with local cache)
  - Collection tracking (set/finish-specific quantities)
  - Treasure Hunt: paste a card list, get a per-location pull list
  - Price tracking via Scryfall
  - SQLite storage, no external dependencies

Run:  python cardvault.py
"""

import sys
from src.db import init_db
from src.menu import main_menu


def main():
    init_db()
    print()
    print("=" * 60)
    print("  CardVault — MTG Collection Manager")
    print("=" * 60)
    print("  Powered by Scryfall (https://scryfall.com)")
    print()
    try:
        main_menu()
    except (KeyboardInterrupt, EOFError):
        print("\n\nGoodbye.")
        sys.exit(0)


if __name__ == "__main__":
    main()
