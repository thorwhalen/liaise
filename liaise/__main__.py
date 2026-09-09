# PYTHON_ARGCOMPLETE_OK
"""``python -m liaise`` — CLI entry point."""

import cw

from liaise.cli import _dispatch_funcs


def main():
    raise SystemExit(cw.dispatch(_dispatch_funcs, prog="liaise"))


if __name__ == "__main__":
    main()
