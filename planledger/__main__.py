"""Single public ingest command:

    python -m planledger incoming.txt

The same command, with only the file name swapped, reads the original pack and every
later message. There are no init/update flags, hints or edits.
"""
import argparse
import sys

from .config import Config
from .pipeline import run_ingest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m planledger",
                                 description="Read a text file of messages and plan/update the hackathon.")
    ap.add_argument("file", help="text file: the original message pack, or a later single message")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return run_ingest(args.file, Config.load())


if __name__ == "__main__":
    sys.exit(main())
