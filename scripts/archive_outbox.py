"""Operator-gated Outbox archive/purge command.

Run with ``PYTHONPATH=core`` (or from the Core image).  The continuity epoch
and safe id are deliberately mandatory for archive mode; this command never
guesses a watermark from row age alone.
"""

from __future__ import annotations

import argparse
import asyncio

from app.db import database
from app.services.outbox import (
    archive_sent_outbox_once,
    purge_archived_outbox_once,
    set_outbox_archive_watermark,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", required=True)
    parser.add_argument("--safe-id", type=int)
    parser.add_argument("--epoch")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--retention-seconds", type=int, default=30 * 24 * 60 * 60)
    parser.add_argument("--purge", action="store_true")
    parser.add_argument("--purge-retention-seconds", type=int, default=90 * 24 * 60 * 60)
    return parser


async def _run(args: argparse.Namespace) -> None:
    await database.connect()
    try:
        if args.safe_id is not None or args.epoch is not None:
            if args.safe_id is None or not args.epoch:
                raise SystemExit("--safe-id and --epoch must be provided together")
            print(
                await set_outbox_archive_watermark(
                    args.stream,
                    args.safe_id,
                    args.epoch,
                )
            )
        print(
            await archive_sent_outbox_once(
                args.stream,
                limit=args.limit,
                retention_seconds=args.retention_seconds,
            )
        )
        if args.purge:
            print(
                {
                    "purged": await purge_archived_outbox_once(
                        args.stream,
                        limit=args.limit,
                        retention_seconds=args.purge_retention_seconds,
                    )
                }
            )
    finally:
        await database.disconnect()


if __name__ == "__main__":
    asyncio.run(_run(_parser().parse_args()))
