"""OIDC provider (Arcane signs in with the hub login): code flow with PKCE, ID token signature, client checks."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from sparkhub import admin_cli, oidc

from .test_auth import enrolled_client, password_step

REDIRECT = "https://llm.example/arcane/auth/oidc/callback"


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def pkce() -> tuple[str, str]:
    v = secrets.token_urlsafe(64)
    return v, oidc.b64u(hashlib.sha256(v.encode()).digest())


def auth_params(challenge: str, **kw) -> dict:
    p = {"response_type": "code", "client_id": "arcane", "redirect_uri": REDIRECT, "scope": "openid email profile",
         "state": "st-1", "nonce": "n-1", "code_challenge": challenge, "code_challenge_method": "S256"}
    p.update(kw)
    return p


def verify_jwt(token: str, jwk: dict) -> dict:
    h, p, s = token.split(".")
    assert json.loads(b64d(h)) == {"alg": "ES256", "typ": "JWT", "kid": jwk["kid"]}
    pub = ec.EllipticCurvePublicNumbers(int.from_bytes(b64d(jwk["x"]), "big"), int.from_bytes(b64d(jwk["y"]), "big"),
                                        ec.SECP256R1()).public_key()
    raw = b64d(s)
    sig = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub.verify(sig, f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256()))     # raises if wrong
    return json.loads(b64d(p))


def setup_client(hub) -> str:
    return oidc.add_client(hub.db, "arcane", [REDIRECT], "test")


def code_from(r) -> dict:
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert loc.startswith(REDIRECT + "?")
    return {k: v[0] for k, v in parse_qs(urlsplit(loc).query).items()}


def test_discovery_and_jwks(hub):
    with hub.client() as c:
        d = c.get("/oidc/.well-known/openid-configuration").json()
        assert d["issuer"] == "https://hub.example/oidc"
        assert d["token_endpoint"] == "https://hub.example/oidc/token"
        assert d["code_challenge_methods_supported"] == ["S256"]
        keys = c.get("/oidc/jwks").json()["keys"]
        assert len(keys) == 1 and keys[0]["alg"] == "ES256" and "d" not in keys[0]
    key = hub.cfg.data_dir / "oidc" / "es256.pem"
    assert key.stat().st_mode & 0o777 == 0o600


def test_full_flow(hub):
    secret = setup_client(hub)
    c, _ = enrolled_client(hub)
    v, ch = pkce()
    q = code_from(c.get("/oidc/authorize", params=auth_params(ch), follow_redirects=False))
    assert q["state"] == "st-1" and q["iss"] == "https://hub.example/oidc"
    with hub.client() as b:   # Arcane's back channel: no cookies
        r = b.post("/oidc/token", auth=("arcane", secret),
                   data={"grant_type": "authorization_code", "code": q["code"], "redirect_uri": REDIRECT,
                         "code_verifier": v})
        assert r.status_code == 200, r.text
        assert r.headers["cache-control"] == "no-store"
        tok = r.json()
        jwk = b.get("/oidc/jwks").json()["keys"][0]
        claims = verify_jwt(tok["id_token"], jwk)
        assert claims["iss"] == "https://hub.example/oidc" and claims["aud"] == "arcane"
        assert claims["nonce"] == "n-1" and claims["preferred_username"] == "admin"
        assert claims["groups"] == ["sparkhub-admin"] and len(claims["sub"]) == 32
        ui = b.get("/oidc/userinfo", headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
        assert ui["sub"] == claims["sub"] and ui["preferred_username"] == "admin"
        # the code is single use
        r = b.post("/oidc/token", auth=("arcane", secret),
                   data={"grant_type": "authorization_code", "code": q["code"], "redirect_uri": REDIRECT,
                         "code_verifier": v})
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
        assert b.get("/oidc/userinfo", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert hub.db.one("SELECT 1 FROM events WHERE kind='oidc_login'")


def test_client_secret_post_and_bad_secrets(hub):
    secret = setup_client(hub)
    c, _ = enrolled_client(hub)
    with hub.client() as b:
        for verifier_ok, client_secret, want in ((True, "wrong", 401), (False, secret, 400), (True, secret, 200)):
            v, ch = pkce()
            q = code_from(c.get("/oidc/authorize", params=auth_params(ch), follow_redirects=False))
            r = b.post("/oidc/token", data={"grant_type": "authorization_code", "code": q["code"],
                                            "redirect_uri": REDIRECT, "client_id": "arcane",
                                            "client_secret": client_secret,
                                            "code_verifier": v if verifier_ok else v + "x"})
            assert r.status_code == want, (verifier_ok, client_secret == secret, r.text)


def test_redirect_uri_and_request_checks(hub):
    setup_client(hub)
    c, _ = enrolled_client(hub)
    _, ch = pkce()
    # unregistered redirect URI or client: an error page, never a redirect
    r = c.get("/oidc/authorize", params=auth_params(ch, redirect_uri="https://evil.example/cb"),
              follow_redirects=False)
    assert r.status_code == 400
    r = c.get("/oidc/authorize", params=auth_params(ch, client_id="other"), follow_redirects=False)
    assert r.status_code == 400
    # PKCE is required, and so is the openid scope
    assert code_from(c.get("/oidc/authorize", params=auth_params("", code_challenge_method=""),
                           follow_redirects=False))["error"] == "invalid_request"
    assert code_from(c.get("/oidc/authorize", params=auth_params(ch, scope="profile"),
                           follow_redirects=False))["error"] == "invalid_scope"


def test_login_first_then_back(hub):
    setup_client(hub)
    c, _ = enrolled_client(hub)
    _, ch = pkce()
    with hub.client() as fresh:
        r = fresh.get("/oidc/authorize", params=auth_params(ch, prompt="none"), follow_redirects=False)
        assert code_from(r)["error"] == "login_required"
        r = fresh.get("/oidc/authorize", params=auth_params(ch), follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=%2Foidc%2Fauthorize%3Fpending%3D")
        nxt = parse_qs(urlsplit(r.headers["location"]).query)["next"][0]
    # the logged-in browser follows the saved request: it gets a code for the original parameters
    q = code_from(c.get(nxt, follow_redirects=False))
    assert q["state"] == "st-1" and q["code"]
    # a saved request works once
    assert c.get(nxt, follow_redirects=False).status_code == 400


def test_password_stage_and_non_admin(hub):
    setup_client(hub)
    c, _ = enrolled_client(hub)
    _, ch = pkce()
    with hub.client() as half:
        password_step(half)            # password done, TOTP not yet
        r = half.get("/oidc/authorize", params=auth_params(ch), follow_redirects=False)
        assert r.headers["location"].startswith("/login?next=")
    hub.db.execute("UPDATE users SET role='infer' WHERE username='admin'")
    r = c.get("/oidc/authorize", params=auth_params(ch), follow_redirects=False)
    assert code_from(r)["error"] == "access_denied"           # Arcane: admins only
    # chat-ui is open to every role, and a non-admin gets no admin group
    oidc.add_client(hub.db, "chatui", [REDIRECT], "test")
    r = c.get("/oidc/authorize", params={**auth_params(ch), "client_id": "chatui"}, follow_redirects=False)
    assert "code" in code_from(r)
    u = dict(hub.db.one("SELECT id, username, role, created_at FROM users WHERE username='admin'"))
    from types import SimpleNamespace
    from sparkhub.oidc import Provider
    assert Provider.claims(SimpleNamespace(subject=Provider.subject), u, ["groups"])["groups"] == ["sparkhub-user"]


def test_cli_and_validation(hub, capsys, monkeypatch):
    monkeypatch.setattr(admin_cli, "load_config", lambda: hub.cfg)
    monkeypatch.setattr(admin_cli, "DB", lambda path: hub.db)
    assert admin_cli.main(["oidc", "add-client", "arcane", "--redirect-uri", REDIRECT]) == 0
    out = capsys.readouterr().out
    assert "https://hub.example/oidc" in out
    assert admin_cli.main(["oidc", "list"]) == 0 and REDIRECT in capsys.readouterr().out
    assert admin_cli.main(["oidc", "add-client", "x", "--redirect-uri", "http://evil.example/cb"]) == 2
    assert admin_cli.main(["oidc", "remove", "arcane"]) == 0
    assert oidc.list_clients(hub.db) == []
