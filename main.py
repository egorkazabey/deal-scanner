"""Entry file for hosting that asks for a file to run: `python main.py` = `python -m scanner bot`.

Other commands work too: `python main.py run-once --dry`.
"""

import sys

from scanner.__main__ import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("bot")
    main()
