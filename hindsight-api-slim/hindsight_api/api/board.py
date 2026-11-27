"""Fork-only addition: lightweight built-in memory board UI (hindsight-board).

Serves static assets from ``hindsight_api/board_static`` under ``/ui``, same
origin as the HTTP API, so the board can call the API with relative URLs and
needs no separate process, no CORS handling, and no proxy. The engine and all
upstream routes are untouched: this module only mounts a static directory.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
from pathlib import Path

import aiohttp
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..engine.aiohttp_session import (
    LoopLocalSession,
    UpstreamHTTPError,
    per_phase_timeout,
    raise_for_status,
)

BOARD_DIR = Path(__file__).resolve().parent.parent / "board_static"


class BoardStaticFiles(StaticFiles):
    """StaticFiles with hardened response headers (fork-only).

    The CSP keeps inline script/style (the board is a single-file app) while
    restricting sources. Deliberately NO ``frame-ancestors`` /
    ``X-Frame-Options``: the DSH shell embeds /ui in an iframe from a
    different origin (127.0.0.1:59455) and must keep working.
    ``connect-src`` allows the board's no-cors LAN dependency probes.
    """

    CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self' http://127.0.0.1:* http://192.168.1.14:*; "
        "object-src 'none'; "
        "base-uri 'self'"
    )

    async def get_response(self, path, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers.setdefault("Content-Security-Policy", self.CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        # ETag/Last-Modified alone let browsers apply heuristic freshness and
        # serve a stale cached copy without revalidating (observed in the
        # Electron renderer: a redeployed board kept showing the old UI even
        # across app restarts, while a fresh browser showed the new one).
        # no-cache forces revalidation on every use; ETag keeps 304s cheap.
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


def _hkcu_env(name: str) -> str | None:
    """Read a user-level env value from HKCU\\Environment (staged value)."""
    try:  # pragma: no cover - winreg only exists on Windows
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            val, _ = winreg.QueryValueEx(k, name)
            s = str(val).strip()
            return s or None
    except Exception:
        return None


def _hkcu_set(name: str, value: str) -> None:
    """Write a user-level env value to HKCU\\Environment (setx equivalent)."""
    import winreg  # pragma: no cover - Windows only path

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)


def _llm_current_payload() -> dict:
    from ..config import get_config

    cfg = get_config()
    model = getattr(cfg, "llm_model", None)
    base = getattr(cfg, "llm_base_url", None)
    has_key = bool(getattr(cfg, "llm_api_key", None))
    staged_model = _hkcu_env("LLM_MODEL") or _hkcu_env("HINDSIGHT_API_LLM_MODEL")
    dprov = getattr(cfg, "decision_model_provider", None)
    dmodel = getattr(cfg, "decision_model_model", None)
    dbase = getattr(cfg, "decision_model_base_url", None)
    dkey = getattr(cfg, "decision_model_api_key", None)
    staged_dprov = _hkcu_env("HINDSIGHT_API_DECISION_MODEL_PROVIDER")
    decision = {
        "provider": dprov,
        "model": dmodel,
        "base_url": dbase,
        "has_key": bool(dkey),
        "staged": {
            "provider": staged_dprov,
            "model": _hkcu_env("HINDSIGHT_API_DECISION_MODEL_MODEL"),
            "base_url": _hkcu_env("HINDSIGHT_API_DECISION_MODEL_BASE_URL"),
        },
        "staged_differs": bool(staged_dprov and staged_dprov.lower() != str(dprov or "").lower()),
        "hint": "provider=none 表示未启用独立决策模型，相关操作走主聊天模型；hindsight/typesafe 走 System One 协议（POST {base_url}/v1/systemone）。",
    }
    return {
        "provider": getattr(cfg, "llm_provider", None),
        "model": model,
        "base_url": base,
        "has_key": has_key,
        "staged_model": staged_model,
        "staged_differs": bool(staged_model and model and staged_model != model),
        "decision": decision,
        "hint": "当前生效=进程内已构建配置；staged=HKCU\\Environment 登记值（重启 DeepSeek Harness 后生效）。"
        "若由助手代重启，重启时必须显式注入新 env（进程 env 优先级高于 profile 文件）。",
    }


_DECISION_PROVIDER_ENV = "HINDSIGHT_API_DECISION_MODEL_PROVIDER"
_DECISION_MODEL_ENV = "HINDSIGHT_API_DECISION_MODEL_MODEL"
_DECISION_BASE_ENV = "HINDSIGHT_API_DECISION_MODEL_BASE_URL"


def _llm_select_decision_payload(payload: dict):
    """Stage the decision-model choice into HKCU\\Environment (restart-required).

    Whitelisted keys only: provider (required), model and base_url (optional).
    The API key is deliberately NOT writable here (same discipline as the main
    model slot).
    """
    from ..config import DECISION_MODEL_PROVIDERS

    body = payload or {}
    provider = str(body.get("provider") or "").strip().lower()
    model = str(body.get("model") or "").strip()
    base = str(body.get("base_url") or "").strip()
    if provider not in DECISION_MODEL_PROVIDERS:
        return JSONResponse(
            {"detail": f"provider 必须是 {'/'.join(DECISION_MODEL_PROVIDERS)} 之一"},
            status_code=400,
        )
    if base and not base.lower().startswith(("http://", "https://")):
        return JSONResponse({"detail": "base_url 必须是 http(s) 地址"}, status_code=400)
    previous = {
        "provider": _hkcu_env(_DECISION_PROVIDER_ENV),
        "model": _hkcu_env(_DECISION_MODEL_ENV),
        "base_url": _hkcu_env(_DECISION_BASE_ENV),
    }
    written = {}
    _hkcu_set(_DECISION_PROVIDER_ENV, provider)
    written[_DECISION_PROVIDER_ENV] = provider
    if model:
        _hkcu_set(_DECISION_MODEL_ENV, model)
        written[_DECISION_MODEL_ENV] = model
    if base:
        _hkcu_set(_DECISION_BASE_ENV, base.rstrip("/"))
        written[_DECISION_BASE_ENV] = base.rstrip("/")
    return {
        "staged": True,
        "written": written,
        "previous": previous,
        "restart_required": True,
        "hint": "已登记到 HKCU\\Environment（用户级）。重启 DeepSeek Harness 后新进程读取生效；"
        "进程 env 优先级陷阱同主模型（助手代重启须显式注入）。API key 如需更换请手动 setx HINDSIGHT_API_DECISION_MODEL_API_KEY。",
    }


_board_http = LoopLocalSession(timeout=per_phase_timeout(8.0))


async def _llm_models_payload(payload: dict):
    """Proxy {base_url}/models server-side. POST-only => guarded by the
    OriginAllowlistMiddleware. Optional API key is used in-flight only and is
    never stored, logged or returned."""
    base = str((payload or {}).get("base_url") or "").strip().rstrip("/")
    key = str((payload or {}).get("key") or "").strip()
    if not base.lower().startswith(("http://", "https://")):
        return JSONResponse({"detail": "base_url 必须是 http(s) 地址"}, status_code=400)
    url = base + "/models"
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with _board_http.get().get(url, headers=headers) as resp:
            await raise_for_status(resp)
            raw = await resp.content.read(1024 * 1024 + 1)
    except UpstreamHTTPError as e:
        what = {
            401: "鉴权失败（401）：检查 API key",
            403: "被拒绝（403）",
            404: "该端点未提供 /models（404）",
        }.get(e.status_code)
        return JSONResponse({"detail": what or f"上游返回 HTTP {e.status_code}"}, status_code=502)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return JSONResponse({"detail": "连不上 base_url（超时或网络不可达）"}, status_code=502)
    except Exception:
        logging.exception("board /ui-ext/llm/models proxy failed")
        return JSONResponse({"detail": "模型列表获取失败"}, status_code=502)
    if len(raw) > 1024 * 1024:
        return JSONResponse({"detail": "响应超过 1MB 上限"}, status_code=502)
    try:
        data = _json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return JSONResponse({"detail": "响应不是合法 JSON"}, status_code=502)
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = []
    models = sorted({str(m.get("id")) for m in items if isinstance(m, dict) and m.get("id")})
    return {"base_url": base, "models": models, "count": len(models)}


def _llm_select_payload(payload: dict):
    """Stage the chat-model choice into HKCU\\Environment (restart-required).

    Only whitelisted keys are written: LLM_MODEL (required) and optionally
    LLM_BASE_URL. Provider / API key are deliberately NOT writable here.
    """
    model = str((payload or {}).get("model") or "").strip()
    base = str((payload or {}).get("base_url") or "").strip()
    if not model:
        return JSONResponse({"detail": "model 不能为空"}, status_code=400)
    if base and not base.lower().startswith(("http://", "https://")):
        return JSONResponse({"detail": "base_url 必须是 http(s) 地址"}, status_code=400)
    previous = {"LLM_MODEL": _hkcu_env("LLM_MODEL") or _hkcu_env("HINDSIGHT_API_LLM_MODEL")}
    _hkcu_set("LLM_MODEL", model)
    if base:
        _hkcu_set("LLM_BASE_URL", base.rstrip("/"))
    return {
        "staged": True,
        "written": {"LLM_MODEL": model, **({"LLM_BASE_URL": base.rstrip("/")} if base else {})},
        "previous": previous,
        "restart_required": True,
        "hint": "已登记到 HKCU\\Environment（用户级）。重启 DeepSeek Harness 后新进程读取注册表生效；"
        "若由助手代重启，必须显式注入新 env（进程 env 优先级陷阱）。API key 如需更换请手动 setx HINDSIGHT_API_LLM_API_KEY。",
    }


def mount_board(app: FastAPI) -> None:
    """Mount the board UI at /ui when its static directory is present.

    Kept deliberately passive: a missing asset directory logs a warning and
    leaves the API exactly as upstream ships it (the board is a fork-only
    convenience, never a hard dependency).
    """
    if not (BOARD_DIR / "index.html").is_file():
        logging.warning(
            "Board UI assets missing at %s; /ui not mounted (fork-only feature)",
            BOARD_DIR,
        )
        return
    app.mount(
        "/ui",
        BoardStaticFiles(directory=str(BOARD_DIR), html=True),
        name="board",
    )
    # Fork-only board extension endpoints (same origin as /ui, POST guarded by
    # the upstream OriginAllowlistMiddleware).
    app.add_api_route("/ui-ext/llm/current", lambda: _llm_current_payload(), methods=["GET"])
    app.add_api_route("/ui-ext/llm/models", _llm_models_payload, methods=["POST"])
    app.add_api_route("/ui-ext/llm/select", _llm_select_payload, methods=["POST"])
    app.add_api_route("/ui-ext/llm/select-decision", _llm_select_decision_payload, methods=["POST"])
    logging.info("Board UI mounted at /ui (%s)", BOARD_DIR)
