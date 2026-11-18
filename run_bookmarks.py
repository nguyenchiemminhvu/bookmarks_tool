"""PyInstaller entry point (bookmarks/__main__.py uses relative imports, so it can't be the script itself)."""
import sys

from bookmarks.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
