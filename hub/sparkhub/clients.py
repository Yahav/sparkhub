"""Client downloads: OpenCode config generated from the live settings, the skill, the CLI and the CA
certificate. None of these hold secrets: the config reads the API key from ~/.config/sparkhub/key (written once by
`sparkhub install-opencode`, mode 600), so no environment variable is needed per session."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

KEY_REF = "{file:~/.config/sparkhub/key}"   # OpenCode substitutes the file's content (checked with v2.0.24)

# opencode only compacts when it knows the window; the benchmark ran with output 32000
OPENCODE_OUTPUT = 32000
MCP_TIMEOUT_MS = 20 * 60 * 1000
CLI_DEFAULT_URL = "https://sparkhub.example"   # the literal default in clients/sparkhub; replaced on download


def base_url(request: Request) -> str:
    host = request.headers.get("host", "localhost")
    return f"https://{host}"


def opencode_config(hub, base: str) -> dict:
    models = {}
    ctx = {m: hub.presets.settings(m)["max_model_len"] for m in hub.presets.available}
    titles = {"auto": "SparkHub (whatever is loaded)", "fast": "SparkHub Fast (qwen36)",
              "quality": "SparkHub Quality (Flash)"}
    if "custom" in hub.presets:
        titles["custom"] = f"SparkHub Custom ({hub.presets['custom'].served_name})"
    for alias in ("auto", *hub.presets.available):
        window = min(ctx.values()) if alias == "auto" else ctx[alias]
        models[alias] = {
            "name": titles[alias],
            "limit": {"context": window, "output": min(OPENCODE_OUTPUT, window // 4)},
            "tool_call": True,
            # OpenCode only forwards known options from variants; reasoningEffort arrives as reasoning_effort,
            # which the hub turns into thinking on (checked with v2.0.24). No variant = thinking off.
            # Defining any variant stops OpenCode adding its own low/medium/high; every key listed here becomes
            # a menu entry (a "disabled" flag would not hide it), so only "thinking" is listed.
            "variants": {"thinking": {"reasoningEffort": "high"}},
        }
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"sparkhub": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "SparkHub",
            "options": {"baseURL": f"{base}/v1", "apiKey": KEY_REF, "timeout": 1800000},
            "models": models,
        }},
        "mcp": {"sparkhub": {
            "type": "remote", "url": f"{base}/mcp", "enabled": True, "oauth": False,
            "headers": {"Authorization": f"Bearer {KEY_REF}"},
            "timeout": MCP_TIMEOUT_MS,
        }},
        "model": "sparkhub/auto",
    }


def install(app, clients_dir: Path) -> None:
    hub = app.state.hub
    cfg = app.state.cfg
    router = APIRouter(prefix="/clients")    # per app: the handlers close over this app's hub

    @router.get("/opencode.json")
    async def oc(request: Request):
        return JSONResponse(opencode_config(hub, base_url(request)))

    @router.get("/opencode-skill.md")
    async def skill():
        return PlainTextResponse((clients_dir / "opencode-skill.md").read_text(), media_type="text/markdown")

    @router.get("/sparkhub")
    async def cli(request: Request):
        # the CLI's default URL becomes the address it was downloaded from
        src = (clients_dir / "sparkhub").read_text().replace(f'"{CLI_DEFAULT_URL}"', f'"{base_url(request)}"')
        return PlainTextResponse(src, media_type="text/x-python",
                                 headers={"Content-Disposition": 'attachment; filename="sparkhub"'})

    @router.get("/ca.crt")
    async def ca():
        p = cfg.data_dir / "tls" / "ca.crt"       # the certificate only; the CA key never leaves data/tls
        if not p.exists():
            return PlainTextResponse("no local CA in use", status_code=404)
        return FileResponse(p, media_type="application/x-x509-ca-cert", filename="sparkhub-ca.crt")

    app.include_router(router)
