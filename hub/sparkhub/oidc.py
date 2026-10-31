"""OpenID Connect provider: apps behind the hub (Arcane) sign in with the SparkHub login, so there is no second
username and password.

Authorization code flow only, with PKCE (S256) required. Clients are confidential: they authenticate to the token
endpoint with a secret, using HTTP Basic or form fields. ID tokens are signed with ES256.
- **Signing key:** `data/oidc/es256.pem`, created on first start (mode 600, never leaves the Spark).
- **Clients:** registered with `sparkhub-admin oidc add-client NAME --redirect-uri URL`. The secret is shown once,
  and only its hash is stored.
- **Who gets a code:** only an admin with a full hub session (password + TOTP). Anyone else is sent to the hub's
  login page first.
- **Codes and access tokens:** live in memory, for 60 s and 5 min. A hub restart only means signing in again.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus, urlencode, urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from .db import DB

CODE_TTL = 60
TOKEN_TTL = 300
PENDING_TTL = 900
ID_TOKEN_TTL = 300
ADMIN_GROUP = "sparkhub-admin"
USER_GROUP = "sparkhub-user"
OPEN_CLIENTS = {"chatui"}        # apps every role may sign in to (chat is using the models); the rest: admins only
SCOPES = ("openid", "profile", "email", "groups")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ---------------------------------------------------------------------- clients
def add_client(db: DB, client_id: str, redirect_uris: list[str], actor: str) -> str:
    """Register (or re-key) a client. Returns the new secret, shown once."""
    if not client_id or len(client_id) > 64 or not client_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("client id: 1-64 letters, digits, - _")
    if not redirect_uris:
        raise ValueError("at least one --redirect-uri")
    for u in redirect_uris:
        p = urlsplit(u)
        if p.scheme != "https" and not (p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost")):
            raise ValueError(f"redirect URI must be https (or http on localhost): {u}")
        if p.fragment or not p.netloc:
            raise ValueError(f"redirect URI must be absolute, without a fragment: {u}")
    secret = secrets.token_urlsafe(32)
    db.execute("INSERT INTO oidc_clients(id, secret_hash, redirect_uris, created_at) VALUES(?,?,?,?)"
               " ON CONFLICT(id) DO UPDATE SET secret_hash=excluded.secret_hash,"
               " redirect_uris=excluded.redirect_uris, created_at=excluded.created_at",
               (client_id, _sha(secret), json.dumps(redirect_uris), time.time()))
    db.event("oidc_client_set", actor, client=client_id, redirect_uris=redirect_uris)
    return secret


def list_clients(db: DB) -> list[dict[str, Any]]:
    return [{"id": r["id"], "redirect_uris": json.loads(r["redirect_uris"]), "created_at": r["created_at"]}
            for r in db.all("SELECT * FROM oidc_clients ORDER BY id")]


def remove_client(db: DB, client_id: str, actor: str) -> bool:
    n = db.execute("DELETE FROM oidc_clients WHERE id=?", (client_id,)).rowcount
    if n:
        db.event("oidc_client_removed", actor, client=client_id)
    return bool(n)


# ---------------------------------------------------------------------- signing key
class Signer:
    def __init__(self, key_path: Path):
        if key_path.exists():
            self.key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        else:
            key_path.parent.mkdir(parents=True, exist_ok=True)
            self.key = ec.generate_private_key(ec.SECP256R1())
            pem = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption())
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
        nums = self.key.public_key().public_numbers()
        x, y = nums.x.to_bytes(32, "big"), nums.y.to_bytes(32, "big")
        # RFC 7638 thumbprint as the key id
        thumb = json.dumps({"crv": "P-256", "kty": "EC", "x": b64u(x), "y": b64u(y)}, separators=(",", ":"))
        self.kid = b64u(hashlib.sha256(thumb.encode()).digest())[:16]
        self.jwk = {"kty": "EC", "crv": "P-256", "x": b64u(x), "y": b64u(y), "use": "sig", "alg": "ES256",
                    "kid": self.kid}

    def sign(self, claims: dict[str, Any]) -> str:
        header = {"alg": "ES256", "typ": "JWT", "kid": self.kid}
        signing_input = (b64u(json.dumps(header, separators=(",", ":")).encode()) + "." +
                         b64u(json.dumps(claims, separators=(",", ":")).encode()))
        r, s = decode_dss_signature(self.key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
        return signing_input + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


# ---------------------------------------------------------------------- provider
@dataclass
class Grant:
    client_id: str
    redirect_uri: str
    user_id: int
    scope: list[str]
    nonce: str | None
    challenge: str
    auth_time: int
    expires: float


class Provider:
    def __init__(self, db: DB, issuer: str, key_path: Path):
        self.db = db
        self.issuer = issuer.rstrip("/")
        self.signer = Signer(key_path)
        self.codes: dict[str, Grant] = {}
        self.tokens: dict[str, tuple[int, list[str], float, str]] = {}   # hash -> (user id, scope, expires, client)
        self.pending: dict[str, tuple[dict[str, str], float]] = {}    # authorize requests waiting for a login

    def discovery(self) -> dict[str, Any]:
        i = self.issuer
        return {"issuer": i, "authorization_endpoint": f"{i}/authorize", "token_endpoint": f"{i}/token",
                "userinfo_endpoint": f"{i}/userinfo", "jwks_uri": f"{i}/jwks",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code"],
                "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["ES256"],
                "scopes_supported": list(SCOPES), "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
                "claims_supported": ["sub", "iss", "aud", "exp", "iat", "auth_time", "nonce", "name",
                                     "preferred_username", "groups"]}

    def _prune(self) -> None:
        now = time.time()
        for d in (self.codes, self.pending):
            for k in [k for k, v in d.items() if (v.expires if isinstance(v, Grant) else v[1]) < now]:
                d.pop(k, None)
        for k in [k for k, v in self.tokens.items() if v[2] < now]:
            self.tokens.pop(k, None)

    def client(self, client_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM oidc_clients WHERE id=?", (client_id or "",))
        if row is None:
            return None
        return {"id": row["id"], "secret_hash": row["secret_hash"], "redirect_uris": json.loads(row["redirect_uris"])}

    def user(self, user_id: int, client_id: str) -> dict[str, Any] | None:
        """The user, if they may sign in to this client: any active user for an open client, else admins only."""
        row = self.db.one("SELECT id, username, role, disabled, created_at FROM users WHERE id=?", (user_id,))
        if row is None or row["disabled"] or (row["role"] != "admin" and client_id not in OPEN_CLIENTS):
            return None
        return dict(row)

    @staticmethod
    def subject(u: dict[str, Any]) -> str:
        # stable per account; a deleted and re-created user with the same name is a new subject
        return _sha(f"{u['id']}:{u['created_at']}")[:32]

    def claims(self, u: dict[str, Any], scope: list[str]) -> dict[str, Any]:
        c: dict[str, Any] = {"sub": self.subject(u)}
        if "profile" in scope:
            c.update(name=u["username"], preferred_username=u["username"])
        if "groups" in scope or "profile" in scope:
            c["groups"] = [ADMIN_GROUP] if u["role"] == "admin" else [USER_GROUP]
        return c

    def new_code(self, params: dict[str, str], user_id: int) -> str:
        self._prune()
        code = secrets.token_urlsafe(32)
        self.codes[_sha(code)] = Grant(params["client_id"], params["redirect_uri"], user_id,
                                       params["scope"].split(), params.get("nonce") or None,
                                       params["code_challenge"], int(time.time()), time.time() + CODE_TTL)
        return code

    def save_pending(self, params: dict[str, str]) -> str:
        self._prune()
        if len(self.pending) > 1000:
            self.pending.clear()
        pid = secrets.token_urlsafe(16)
        self.pending[pid] = (params, time.time() + PENDING_TTL)
        return pid

    def take_pending(self, pid: str) -> dict[str, str] | None:
        item = self.pending.pop(pid or "", None)
        return item[0] if item and item[1] > time.time() else None

    def exchange(self, client_id: str, code: str, redirect_uri: str, verifier: str) -> dict[str, Any] | str:
        """Token response, or an OAuth error code."""
        grant = self.codes.pop(_sha(code or ""), None)     # single use, even when the checks below fail
        if grant is None or grant.expires < time.time():
            return "invalid_grant"
        if grant.client_id != client_id or grant.redirect_uri != redirect_uri:
            return "invalid_grant"
        if not verifier or not secrets.compare_digest(b64u(hashlib.sha256(verifier.encode()).digest()),
                                                       grant.challenge):
            return "invalid_grant"
        u = self.user(grant.user_id, grant.client_id)
        if u is None:
            return "invalid_grant"
        now = int(time.time())
        id_claims = {"iss": self.issuer, "aud": client_id, "azp": client_id, "iat": now, "exp": now + ID_TOKEN_TTL,
                     "auth_time": grant.auth_time, **self.claims(u, grant.scope)}
        if grant.nonce:
            id_claims["nonce"] = grant.nonce
        access = secrets.token_urlsafe(32)
        self.tokens[_sha(access)] = (u["id"], grant.scope, time.time() + TOKEN_TTL, client_id)
        self.db.event("oidc_login", f"user:{u['username']}", client=client_id)
        return {"access_token": access, "token_type": "Bearer", "expires_in": TOKEN_TTL,
                "id_token": self.signer.sign(id_claims), "scope": " ".join(grant.scope)}

    def userinfo(self, access: str) -> dict[str, Any] | None:
        item = self.tokens.get(_sha(access or ""))
        if item is None or item[2] < time.time():
            return None
        u = self.user(item[0], item[3])
        return self.claims(u, item[1]) if u else None


def _client_auth(request: Request, form: dict[str, str]) -> tuple[str, str]:
    h = request.headers.get("authorization") or ""
    if h.lower().startswith("basic "):
        try:
            raw = base64.b64decode(h[6:].strip()).decode()
            cid, _, sec = raw.partition(":")
            return unquote_plus(cid), unquote_plus(sec)
        except Exception:
            return "", ""
    return form.get("client_id", ""), form.get("client_secret", "")


def install(app: FastAPI) -> None:
    cfg = app.state.cfg
    db: DB = app.state.db
    provider = Provider(db, cfg.oidc_issuer, cfg.data_dir / "oidc" / "es256.pem")
    app.state.oidc = provider
    router = APIRouter(prefix="/oidc")
    nostore = {"Cache-Control": "no-store", "Pragma": "no-cache"}

    def err_redirect(params: dict[str, str], error: str) -> Response:
        q = {"error": error, "iss": provider.issuer}
        if params.get("state"):
            q["state"] = params["state"]
        return RedirectResponse(params["redirect_uri"] + ("&" if "?" in params["redirect_uri"] else "?") +
                                urlencode(q), status_code=303)

    @router.get("/.well-known/openid-configuration")
    async def discovery():
        return JSONResponse(provider.discovery())

    @router.get("/jwks")
    async def jwks():
        return JSONResponse({"keys": [provider.signer.jwk]})

    @router.get("/authorize")
    async def authorize(request: Request):
        q = dict(request.query_params)
        if q.get("pending"):
            q = provider.take_pending(q["pending"]) or {}
            if not q:
                return PlainTextResponse("This sign-in request expired. Go back to the app and try again.",
                                         status_code=400)
        params = {k: q.get(k, "") for k in ("response_type", "client_id", "redirect_uri", "scope", "state", "nonce",
                                            "code_challenge", "code_challenge_method", "prompt")}
        client = provider.client(params["client_id"])
        # never redirect to an unverified URI: these two errors are shown here
        if client is None:
            return PlainTextResponse("Unknown client.", status_code=400)
        if params["redirect_uri"] not in client["redirect_uris"]:
            return PlainTextResponse("This redirect URI is not registered for the client.", status_code=400)
        if params["response_type"] != "code":
            return err_redirect(params, "unsupported_response_type")
        if "openid" not in params["scope"].split():
            return err_redirect(params, "invalid_scope")
        if params["code_challenge_method"] != "S256" or not (43 <= len(params["code_challenge"]) <= 128):
            return err_redirect(params, "invalid_request")
        params["scope"] = " ".join(s for s in params["scope"].split() if s in SCOPES)
        s = app.state.auth.session(request)
        if s is None or s.stage != "full":
            if params["prompt"] == "none":
                return err_redirect(params, "login_required")
            pid = provider.save_pending(params)
            return RedirectResponse("/login?" + urlencode({"next": f"/oidc/authorize?pending={pid}"}),
                                    status_code=303)
        if provider.user(s.user_id, params["client_id"]) is None:
            return err_redirect(params, "access_denied")
        code = provider.new_code(params, s.user_id)
        q = {"code": code, "iss": provider.issuer}
        if params["state"]:
            q["state"] = params["state"]
        sep = "&" if "?" in params["redirect_uri"] else "?"
        return RedirectResponse(params["redirect_uri"] + sep + urlencode(q), status_code=303, headers=nostore)

    @router.post("/token")
    async def token(request: Request):
        form = {k: str(v) for k, v in (await request.form()).items()}
        cid, sec = _client_auth(request, form)
        client = provider.client(cid)
        if client is None or not secrets.compare_digest(_sha(sec), client["secret_hash"]):
            db.event("oidc_client_auth_failed", None, client=cid[:64])
            return JSONResponse({"error": "invalid_client"}, status_code=401,
                                headers={**nostore, "WWW-Authenticate": 'Basic realm="sparkhub"'})
        if form.get("grant_type") != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400, headers=nostore)
        out = provider.exchange(cid, form.get("code", ""), form.get("redirect_uri", ""),
                                form.get("code_verifier", ""))
        if isinstance(out, str):
            return JSONResponse({"error": out}, status_code=400, headers=nostore)
        return JSONResponse(out, headers=nostore)

    @router.api_route("/userinfo", methods=["GET", "POST"])
    async def userinfo(request: Request):
        h = request.headers.get("authorization") or ""
        claims = provider.userinfo(h[7:].strip()) if h.lower().startswith("bearer ") else None
        if claims is None:
            return JSONResponse({"error": "invalid_token"}, status_code=401,
                                headers={**nostore, "WWW-Authenticate": 'Bearer error="invalid_token"'})
        return JSONResponse(claims, headers=nostore)

    app.include_router(router)
