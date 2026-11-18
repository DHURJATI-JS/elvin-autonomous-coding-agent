"""Installation settings from a terminal - the same switches the administrator
has on the System Status page. The running app applies a change within a minute.

    docker compose exec app python -m elvin.admin show
    docker compose exec app python -m elvin.admin gemma-source local   # use the local Docker Gemma for now
    docker compose exec app python -m elvin.admin gemma-source auto    # gateway first again
    docker compose exec app python -m elvin.admin gateway http://192.168.1.50:8000/v1
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from . import db as dbm
from .config import get_settings
from .services import app_settings
from .services.ai import router as ai


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m elvin.admin", description="Change Elvin's installation settings.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="print the current settings")
    g = sub.add_parser("gemma-source", help="auto = gateway first, local Docker Gemma when it is down; local = local Docker Gemma only")
    g.add_argument("source", choices=ai.GEMMA_SOURCES)
    gw = sub.add_parser("gateway", help="point Elvin at the gateway's address")
    gw.add_argument("url")
    return p


async def run(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    s = get_settings()
    try:
        await dbm.create_all()
        values: dict[str, str] = {}
        if args.cmd == "gemma-source":
            if args.source == "local" and ai.LOCAL_FALLBACK not in ai.registry().providers:
                print("The local Docker Gemma is not set up on this installation (LOCAL_GEMMA_URL is empty).", file=sys.stderr)
                return 2
            values = {"gemma_source": args.source}
        elif args.cmd == "gateway":
            try:
                values = {"gateway_url": ai.normalize_gateway_url(args.url)}
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 2
        if values:
            async with dbm.session() as db:
                await app_settings.save(db, values, via="terminal")
                await db.commit()
        stored = await app_settings.load()
        ai.set_overrides(stored)
        print(f"gemma source : {ai.gemma_source()}  ({'local Docker Gemma only' if ai.gemma_source() == 'local' else 'gateway first, local Docker Gemma when it is down'})")
        print(f"gateway      : {ai.gateway_url()}")
        print(f"local gemma  : {s.local_gemma_model + ' at ' + s.local_gemma_url if s.local_gemma_url else 'not set up'}"
              f" (effort never above {s.local_gemma_effort_cap})")
        if values:
            print("Saved. The running app applies it within a minute.")
        return 0
    finally:
        await dbm.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(run(sys.argv[1:])))


if __name__ == "__main__":
    main()
