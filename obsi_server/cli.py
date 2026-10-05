"""Command line: `python -m obsi_server serve` and `admin ...` management commands."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .app import build_context
from .config import Config


def _ops(s: str) -> list[str]:
    return [o.strip() for o in s.split(",") if o.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="obsi_server")
    p.add_argument("--data-dir", help="data directory (default: OBSI_DATA_DIR or ./obsi-data)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the HTTP server")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    a = sub.add_parser("admin", help="management commands").add_subparsers(dest="acmd", required=True)
    ci = a.add_parser("create-invite", help="create a one-use user registration invite")
    ci.add_argument("--name", required=True)
    a.add_parser("list-users", help="list registered user accounts")
    a.add_parser("list-invites", help="list registration invites (codes are never shown)")
    t = a.add_parser("create-token")
    t.add_argument("--kind", choices=["device", "mcp", "admin"], required=True)
    t.add_argument("--name", required=True)
    t.add_argument("--vault", action="append", default=[], help="vault id (repeatable)")
    t.add_argument("--ops", default="", help="comma-separated operations for the given vaults")
    v = a.add_parser("create-vault")
    v.add_argument("--name", required=True)
    a.add_parser("list-vaults")
    a.add_parser("list-tokens")
    r = a.add_parser("revoke")
    r.add_argument("token_id")
    g = a.add_parser("grant")
    g.add_argument("token_id")
    g.add_argument("--vault", required=True)
    g.add_argument("--ops", required=True)
    e = a.add_parser("set-embedding")
    e.add_argument("--base-url", required=True)
    e.add_argument("--api-key", default="")
    e.add_argument("--model", required=True)
    for name in ("rag-enable", "rag-disable", "reindex"):
        x = a.add_parser(name)
        x.add_argument("vault")
    return p


def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load()
    if args.data_dir:
        cfg.data_dir = Path(args.data_dir)
    if args.cmd == "serve":
        import uvicorn

        from .app import create_app

        uvicorn.run(create_app(cfg), host=args.host or cfg.host, port=args.port or cfg.port,
                    proxy_headers=True, forwarded_allow_ips="127.0.0.1,::1")
        return 0

    ctx = build_context(cfg)
    try:
        c = args.acmd
        if c == "create-invite":
            invite = ctx.auth.create_invite(args.name)
            _out(invite)
            print("Save the invite code now: it is shown only once.", file=sys.stderr)
        elif c == "list-users":
            _out(ctx.auth.list_users())
        elif c == "list-invites":
            _out(ctx.auth.list_invites())
        elif c == "create-token":
            grants = {vid: _ops(args.ops) for vid in args.vault}
            tok, plain = ctx.auth.create(args.name, args.kind, grants)
            _out({**tok.public(), "token": plain})
            print("Save the token now: it is shown only once.", file=sys.stderr)
        elif c == "create-vault":
            _out(ctx.vaults.create(args.name))
        elif c == "list-vaults":
            _out(ctx.vaults.list())
        elif c == "list-tokens":
            _out([t.public() for t in ctx.auth.list()])
        elif c == "revoke":
            _out(ctx.auth.revoke(args.token_id).public())
        elif c == "grant":
            _out(ctx.auth.grant(args.token_id, args.vault, _ops(args.ops)).public())
        elif c == "set-embedding":
            changed = ctx.rag.set_embedding(args.base_url, args.api_key, args.model)
            print("saved" + ("; model/base_url changed, index will be rebuilt" if changed else ""))
        elif c in ("rag-enable", "rag-disable"):
            _out(ctx.vaults.update(args.vault, rag={"enabled": c == "rag-enable"}))
        elif c == "reindex":
            ctx.rag.reindex_vault(args.vault)
            print("reindex scheduled (the running server embeds in the background)")
    except Exception as ex:  # ApiError and friends
        print(f"error: {ex}", file=sys.stderr)
        return 1
    finally:
        ctx.db.close()
    return 0
