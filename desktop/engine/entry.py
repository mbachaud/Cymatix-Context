"""PyInstaller entry for the bundled engine (cymatix-engine).

A plain script, because PyInstaller runs its entry as __main__, where the
package-relative imports in cymatix_context.desktop_entry would fail.
"""

import sys

from cymatix_context.desktop_entry import main

if __name__ == "__main__":
    sys.exit(main())
