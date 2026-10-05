"""FastAPI app factory: wires services and exposes the REST API and /mcp."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .ai_store import AiStore, norm_path
from .auth import AuthService, Token
from .changes import ChangeQueue
from .config import Config
from .db import Database
from .errors import ApiError
from .mcp import McpServer
from .rag import EmbedFn, RagService, http_embed
from .storage import Storage
from .vaults import VaultService


@dataclass
class Context:
    config: Config
    db: Database
    auth: AuthService
    vaults: VaultService
    storage: Storage
    ai: AiStore
    changes: ChangeQueue
    rag: RagService
    mcp: McpServer


def build_context(config: Config, embed_fn: EmbedFn = http_embed) -> Context:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.data_dir / "obsi.db")
    vaults, changes = VaultService(db), ChangeQueue(db)
    auth, storage = AuthService(db), Storage(db, config.data_dir, config)
    ai = AiStore(db, storage)
    rag = RagService(db, vaults, ai, embed_fn)
    vaults.on_delete += [storage.purge, changes.purge, auth.drop_vault, ai.clear]
    if config.features["rag"]:
        rag.attach()
    return Context(config, db, auth, vaults, storage, ai, changes, rag, McpServer(config, vaults, ai, changes, rag))


# ---- request helpers ---------------------------------------------------------------
def get_ctx(request: Request) -> Context:
    return request.app.state.ctx


def _bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else None


def auth(*kinds: str):
    """Dependency: authenticated, non-revoked token of one of `kinds` (any if empty)."""
    def dep(request: Request) -> Token:
        tok = get_ctx(request).auth.authenticate(_bearer(request))
        if not tok:
            raise ApiError(401, "unauthorized", "missing, invalid or revoked token")
        if kinds and tok.kind not in kinds:
            raise ApiError(403, "forbidden", f"token kind '{tok.kind}' cannot use this endpoint")
        request.state.token = tok
        return tok
    return dep


def vault_access(ctx: Context, tok: Token, vid: str, op: str) -> None:
    vault = ctx.vaults.get(vid)
    if not vault:
        ctx.vaults.require(vid)
    owner_access = tok.kind == "device" and tok.user_id is not None and vault["owner_user_id"] == tok.user_id
    if not owner_access and not tok.can(vid, op):
        raise ApiError(403, "forbidden", f"no '{op}' grant on this vault")


def feature(name: str):
    def dep(request: Request) -> None:
        if not get_ctx(request).config.features[name]:
            raise ApiError(404, "feature_disabled", f"feature '{name}' is disabled")
    return dep


async def read_body(request: Request, limit: int) -> bytes:
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > limit:
        raise ApiError(413, "payload_too_large", f"body exceeds {limit} bytes")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise ApiError(413, "payload_too_large", f"body exceeds {limit} bytes")
    return bytes(buf)


def _int_header(request: Request, name: str) -> int | None:
    v = request.headers.get(name)
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except ValueError:
        raise ApiError(422, "validation_error", f"header {name} must be a number")


# ---- request models ------------------------------------------------------------------
class VaultIn(BaseModel):
    name: str


class RegisterIn(BaseModel):
    invite_code: str
    username: str
    password: str


class LoginIn(BaseModel):
    username: str
    password: str


class RagIn(BaseModel):
    enabled: bool | None = None
    chunk_chars: int | None = None
    chunk_overlap: int | None = None


class VaultPatch(BaseModel):
    name: str | None = None
    rag: RagIn | None = None


class TokenIn(BaseModel):
    name: str
    kind: Literal["device", "mcp", "admin"]
    grants: dict[str, list[str]] = {}


class TokenPatch(BaseModel):
    name: str | None = None
    grants: dict[str, list[str]] | None = None


class EmbeddingIn(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class SettingsIn(BaseModel):
    embedding: EmbeddingIn | None = None


class AckIn(BaseModel):
    status: Literal["applied", "conflict", "rejected"]
    message: str | None = None
    new_version: str | None = None


def _mask(key: str | None) -> str:
    return "" if not key else "****" + (key[-4:] if len(key) > 8 else "")


# ---- routers --------------------------------------------------------------------------
def core_router() -> APIRouter:
    r = APIRouter()

    @r.get("/health")
    def health(request: Request):
        return {"ok": True, "version": __version__, "features": get_ctx(request).config.features}

    @r.get("/me")
    def me(request: Request, tok: Token = Depends(auth())):
        user_row = get_ctx(request).db.one("SELECT * FROM users WHERE id=?", (tok.user_id,)) if tok.user_id else None
        return {"token_id": tok.id, "name": tok.name, "kind": tok.kind, "grants": tok.grants,
                **({"user": AuthService.public_user(user_row)} if user_row else {})}

    @r.get("/vaults")
    def vaults(request: Request, tok: Token = Depends(auth())):
        ctx = get_ctx(request)
        if tok.kind == "admin":
            visible = ctx.vaults.list()
        else:
            ids = {v for v in tok.grants if tok.has_any(v)}
            visible = ctx.vaults.list(ids)
            if tok.user_id:
                owned = ctx.vaults.list_owned(tok.user_id)
                known = {v["id"] for v in visible}
                visible.extend(v for v in owned if v["id"] not in known)
        return {"vaults": [{"id": v["id"], "name": v["name"], "created_at": v["created_at"],
                            "rag": {"enabled": v["rag"]["enabled"]}} for v in visible]}

    @r.post("/vaults")
    def create_user_vault(body: VaultIn, request: Request, tok: Token = Depends(auth("device"))):
        if not tok.user_id:
            raise ApiError(403, "user_account_required", "a registered user device token is required")
        return get_ctx(request).vaults.create(body.name, owner_user_id=tok.user_id)
    return r


def user_auth_router() -> APIRouter:
    r = APIRouter(prefix="/auth")

    @r.post("/register")
    def register(body: RegisterIn, request: Request):
        user, token = get_ctx(request).auth.register(body.invite_code, body.username, body.password)
        return {"token": token, "user": user}

    @r.post("/login")
    def login(body: LoginIn, request: Request):
        user, token = get_ctx(request).auth.login(body.username, body.password)
        return {"token": token, "user": user}
    return r


def admin_router() -> APIRouter:
    r = APIRouter(prefix="/admin", dependencies=[Depends(auth("admin"))])

    @r.get("/users")
    def list_users(request: Request):
        return {"users": get_ctx(request).auth.list_users()}

    @r.post("/invites")
    def create_invite(body: VaultIn, request: Request):
        return get_ctx(request).auth.create_invite(body.name)

    @r.get("/invites")
    def list_invites(request: Request):
        return {"invites": get_ctx(request).auth.list_invites()}

    @r.post("/vaults")
    def create_vault(body: VaultIn, request: Request):
        return get_ctx(request).vaults.create(body.name)

    @r.patch("/vaults/{vid}")
    def patch_vault(vid: str, body: VaultPatch, request: Request):
        rag = body.rag.model_dump(exclude_none=True) if body.rag else None
        return get_ctx(request).vaults.update(vid, body.name, rag)

    @r.delete("/vaults/{vid}", status_code=204)
    def delete_vault(vid: str, request: Request):
        if not get_ctx(request).vaults.delete(vid):
            raise ApiError(404, "not_found", "vault not found")
        return Response(status_code=204)

    @r.post("/vaults/{vid}/reindex")
    def reindex(vid: str, request: Request):
        ctx = get_ctx(request)
        ctx.vaults.require(vid)
        ctx.rag.reindex_vault(vid)
        return {"ok": True, "index": ctx.rag.status()}

    @r.get("/tokens")
    def list_tokens(request: Request):
        return {"tokens": [t.public() for t in get_ctx(request).auth.list()]}

    def _check_vaults(ctx: Context, grants: dict[str, list[str]] | None) -> None:
        for vid in grants or {}:
            if not ctx.vaults.get(vid):
                raise ApiError(422, "unknown_vault", f"unknown vault {vid}")

    @r.post("/tokens")
    def create_token(body: TokenIn, request: Request):
        ctx = get_ctx(request)
        _check_vaults(ctx, body.grants)
        tok, plain = ctx.auth.create(body.name, body.kind, body.grants)
        return {**tok.public(), "token": plain}

    @r.patch("/tokens/{tid}")
    def patch_token(tid: str, body: TokenPatch, request: Request):
        ctx = get_ctx(request)
        _check_vaults(ctx, body.grants)
        return ctx.auth.update(tid, body.name, body.grants).public()

    @r.post("/tokens/{tid}/revoke")
    def revoke(tid: str, request: Request):
        return get_ctx(request).auth.revoke(tid).public()

    def _settings(ctx: Context) -> dict:
        e = ctx.db.get_setting("embedding", {}) or {}
        return {"embedding": {"base_url": e.get("base_url", ""), "api_key": _mask(e.get("api_key")),
                              "model": e.get("model", "")}, "index": ctx.rag.status()}

    @r.get("/settings")
    def get_settings(request: Request):
        return _settings(get_ctx(request))

    @r.put("/settings")
    def put_settings(body: SettingsIn, request: Request):
        ctx = get_ctx(request)
        if body.embedding:
            e = body.embedding
            key = None if (e.api_key or "").startswith("****") else e.api_key  # masked value = unchanged
            ctx.rag.set_embedding(e.base_url, key, e.model)
        return _settings(ctx)
    return r


def storage_router() -> APIRouter:
    r = APIRouter(prefix="/vaults/{vid}/files", dependencies=[Depends(auth("device"))])

    def _access(request: Request, vid: str, op: str) -> Context:
        ctx = get_ctx(request)
        vault_access(ctx, request.state.token, vid, op)
        return ctx

    @r.get("")
    def listing(vid: str, request: Request):
        return {"files": _access(request, vid, "read").storage.list(vid)}

    def _headers(o: dict) -> dict:
        return {"ETag": o["etag"], "X-Mtime-Cli": str(o["mtime_cli"]), "X-Ctime-Cli": str(o["ctime_cli"]),
                "X-Mtime-Svr": str(o["mtime_svr"])}

    @r.get("/{key:path}")
    def get_file(vid: str, key: str, request: Request):
        ctx = _access(request, vid, "read")
        o = ctx.storage.stat(vid, key)
        if not o:
            raise ApiError(404, "not_found", "file not found")
        try:
            data = ctx.storage.read(vid, key)
        except FileNotFoundError:
            raise ApiError(404, "not_found", "file not found") from None
        return Response(content=data, media_type="application/octet-stream", headers=_headers(o))

    @r.head("/{key:path}")
    def head_file(vid: str, key: str, request: Request):
        o = _access(request, vid, "read").storage.stat(vid, key)
        if not o:
            return Response(status_code=404)
        return Response(status_code=200, media_type="application/octet-stream",
                        headers={**_headers(o), "Content-Length": str(o["size"])})

    @r.put("/{key:path}")
    async def put_file(vid: str, key: str, request: Request):
        ctx = _access(request, vid, "write")
        data = await read_body(request, ctx.config.max_body_bytes)
        markdown_header = request.headers.get("x-obsi-markdown")
        if markdown_header is not None and markdown_header not in ("true", "false"):
            raise ApiError(422, "validation_error", "X-Obsi-Markdown must be true or false")
        is_markdown = None if markdown_header is None else markdown_header == "true"
        return await run_in_threadpool(ctx.storage.put, vid, key, data, _int_header(request, "x-mtime-cli"),
                                       _int_header(request, "x-ctime-cli"), is_markdown)

    @r.delete("/{key:path}", status_code=204)
    def delete_file(vid: str, key: str, request: Request):
        _access(request, vid, "write").storage.delete(vid, key)
        return Response(status_code=204)
    return r


def ai_router() -> APIRouter:
    r = APIRouter(prefix="/vaults/{vid}", dependencies=[Depends(feature("ai")), Depends(auth("device"))])

    def _access(request: Request, vid: str, op: str) -> Context:
        ctx = get_ctx(request)
        vault_access(ctx, request.state.token, vid, op)
        return ctx

    @r.get("/ai/manifest")
    def manifest(vid: str, request: Request):
        return {"files": _access(request, vid, "read").ai.manifest(vid)}

    @r.put("/ai/files/{path:path}")
    async def ai_put(vid: str, path: str, request: Request):
        ctx = _access(request, vid, "write")
        norm_path(path)
        data = await read_body(request, ctx.config.max_body_bytes)
        return await run_in_threadpool(ctx.ai.put, vid, path, data, _int_header(request, "x-mtime"))

    @r.delete("/ai/files/{path:path}", status_code=204)
    def ai_delete(vid: str, path: str, request: Request):
        _access(request, vid, "write").ai.delete(vid, path)
        return Response(status_code=204)

    @r.post("/ai/clear", status_code=204)
    def ai_clear(vid: str, request: Request):
        _access(request, vid, "write").ai.clear(vid)
        return Response(status_code=204)

    @r.get("/changes")
    def list_changes(vid: str, request: Request, status: str = "pending"):
        return {"changes": _access(request, vid, "write").changes.list(vid, status or None)}

    @r.post("/changes/{cid}/ack")
    def ack(vid: str, cid: str, body: AckIn, request: Request):
        ctx = _access(request, vid, "write")
        return ctx.changes.ack(vid, cid, body.status, body.message, body.new_version)
    return r


def create_app(config: Config | None = None, embed_fn: EmbedFn = http_embed) -> FastAPI:
    config = config or Config.load()
    ctx = build_context(config, embed_fn)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if config.features["rag"] and config.rag_worker:
            ctx.rag.start_worker()
        yield
        ctx.rag.stop_worker()
        ctx.db.close()

    app = FastAPI(title="obsi-mcp", version=__version__, lifespan=lifespan)
    app.state.ctx = ctx
    app.add_middleware(CORSMiddleware, allow_origins=config.cors_origins,
                       allow_methods=["GET", "PUT", "POST", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type", "X-*", "x-mtime", "x-mtime-cli", "x-ctime-cli", "x-obsi-markdown"],
                       expose_headers=["ETag", "X-Mtime-Cli", "X-Ctime-Cli", "X-Mtime-Svr"])

    def err_json(status: int, code: str, message: str, extra: dict | None = None, headers: dict | None = None):
        return JSONResponse({"error": {"code": code, "message": message, **(extra or {})}}, status, headers)

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError):
        hdr = {"WWW-Authenticate": "Bearer"} if e.status == 401 else None
        return err_json(e.status, e.code, e.message, e.extra, hdr)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError):
        msg = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        return err_json(422, "validation_error", msg)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, e: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(e.status_code, "http_error")
        return err_json(e.status_code, code, str(e.detail))

    api = APIRouter(prefix="/api/v1")
    for rt in (core_router(), user_auth_router(), admin_router(), storage_router(), ai_router()):
        api.include_router(rt)
    app.include_router(api)

    @app.api_route("/mcp", methods=["GET"])
    async def mcp_get():
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.post("/mcp")
    async def mcp_post(request: Request):
        if not ctx.config.features["mcp"]:
            raise ApiError(404, "feature_disabled", "feature 'mcp' is disabled")
        tok = ctx.auth.authenticate(_bearer(request))
        if not tok:
            raise ApiError(401, "unauthorized", "missing, invalid or revoked token")
        if tok.kind != "mcp":
            raise ApiError(403, "forbidden", "an mcp token is required")
        try:
            payload = json.loads(await read_body(request, 8 * 1024 * 1024))
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
        res = await run_in_threadpool(ctx.mcp.handle, tok, payload)
        return Response(status_code=202) if res is None else JSONResponse(res)

    return app
