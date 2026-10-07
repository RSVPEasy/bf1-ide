"""bf1 - the command line for bf1-ide. With no arguments it opens the
interactive shell; with a command it runs that and exits (see bf1_cli.py)."""
import sys

from bf1_cli import main

if __name__ == "__main__":
    sys.exit(main())
