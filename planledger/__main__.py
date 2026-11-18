"""CLI entry point for PlanLedger (`python -m planledger <file.txt>`)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

from .config import AppConfig
from .orchestrator import PlanLedgerOrchestrator
from .server import run_server


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    config = AppConfig.from_env()

    if not args or args[0] in ("--serve", "-s"):
        run_server(config)
        return 0

    if args[0] in ("--help", "-h"):
        print(
            "Usage:\n"
            "  python -m planledger <incoming.txt>   Ingest baseline pack or single-message update\n"
            "  python -m planledger                  Launch local Evidence Desk web UI\n",
            flush=True,
        )
        return 0

    if len(args) != 1 or args[0].startswith("-"):
        print(
            "Error: PlanLedger requires exactly one positional file argument (e.g. `python -m planledger incoming.txt`). "
            "No init/update flags or extra arguments are permitted.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    target_path = Path(args[0]).resolve()
    if not target_path.is_file():
        print(
            f"Error: Input file does not exist: {target_path}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    orchestrator = PlanLedgerOrchestrator(config)
    result = orchestrator.ingest_file(target_path)
    print(result["trace_text"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
