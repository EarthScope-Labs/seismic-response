"""``python -m seismic_response.gui [--build DIR]``"""

import argparse
import sys
from pathlib import Path

from .server import build_static, serve


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m seismic_response.gui")
    p.add_argument("--build", metavar="DIR", help="write a static, server-less (Pyodide) copy of the GUI to DIR")
    p.add_argument("files", nargs="*")
    args = p.parse_args()
    if args.build:
        build_static(Path(args.build))
        print(f"Static GUI written to {args.build} (serve it with any web server)")
        return 0
    return serve(files=args.files)


if __name__ == "__main__":
    sys.exit(main())
