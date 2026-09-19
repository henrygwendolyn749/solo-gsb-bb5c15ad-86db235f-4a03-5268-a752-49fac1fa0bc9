"""会话管理接口：创建 / 查询状态 / 重置 / 事件历史 / 请求预演。"""
from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from ..config import settings
from ..db import dumps, get_conn, write_tx
from ..engine import preview
from ..store import store

router = APIRouter(prefix="/__sessions", tags=["sessions"])

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


class CreateSessionBody(BaseModel):
    token: str | None = Field(default=None, description="不传则自动生成")
    scenario: str | None = None
    state: str | None = None
    context: dict = Field(default_factory=dict)


def _session_row(token: str):
    return get_conn().execute(
        """
        SELECT s.token, s.scenario, s.state, s.context, s.created_at, s.updated_at
        FROM sessions s WHERE s.token = ?
        """,
        (token,),
    ).fetchone()


def _serialize(row) -> dict:
    return {
        "token": row["token"],
        "scenario": row["scenario"],
        "state": row["state"],
        "context": json.loads(row["context"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.post("")
async def create_session(body: CreateSessionBody) -> JSONResponse:
    token = body.token or f"sess-{secrets.token_hex(12)}"

    scenario_name = body.scenario
    if scenario_name is None:
        names = store.names()
        if len(names) == 1:
            scenario_name = names[0]
        elif not names:
            return JSONResponse(status_code=400, content={"error": "no_scenario_loaded"})
        else:
            return JSONResponse(status_code=409, content={
                "error": "ambiguous_scenario", "scenarios": names,
                "detail": "存在多个场景，请在 body.scenario 中指定",
            })

    scenario = store.get(scenario_name)
    if scenario is None:
        return JSONResponse(status_code=404, content={"error": "scenario_not_found",
                                              "scenario": scenario_name})

    state = body.state or scenario.initial_state
    if state not in scenario.states:
        return JSONResponse(status_code=422, content={"error": "unknown_state",
                                              "state": state, "states": scenario.states})

    context = dict(body.context)
    context.setdefault("_seq", {})

    with write_tx() as conn:
        try:
            conn.execute(
                "INSERT INTO sessions(token, scenario, state, context) VALUES (?, ?, ?, ?)",
                (token, scenario_name, state, dumps(context)),
            )
        except sqlite3.IntegrityError:
            return JSONResponse(status_code=409, content={"error": "session_exists", "token": token})

    row = _session_row(token)
    return JSONResponse(status_code=201, content=_serialize(row))


@router.get("/{token}")
async def get_session(token: str) -> JSONResponse:
    row = _session_row(token)
    if row is None:
        return JSONResponse(status_code=404, content={"error": "session_not_found", "token": token})
    result = _serialize(row)
    scenario = store.get(row["scenario"])
    if scenario is not None:
        result["available_states"] = scenario.states
        result["routes"] = [
            {"id": r.id, "path": r.path, "methods": r.methods}
            for r in scenario.routes
        ]
    return JSONResponse(result)


@router.get("/{token}/events")
async def list_events(token: str, limit: int = 50) -> JSONResponse:
    if _session_row(token) is None:
        return JSONResponse(status_code=404, content={"error": "session_not_found", "token": token})
    limit = max(1, min(limit, 500))
    rows = get_conn().execute(
        """
        SELECT id, method, path, route, rule, from_state, to_state,
               status_code, created_at
        FROM events WHERE token = ? ORDER BY id DESC LIMIT ?
        """,
        (token, limit),
    ).fetchall()
    return JSONResponse({"token": token, "events": [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# 请求预演：POST /__sessions/{token}/preview
# ---------------------------------------------------------------------------

_SCALAR_TYPES = (str, int, float, bool)


class PreviewBody(BaseModel):
    method: str = Field(description="拟使用的 HTTP 方法")
    path: str = Field(description="拟请求的业务路径，如 /orders/42/pay")
    query: dict[str, Any] = Field(
        default_factory=dict,
        description="查询参数；值支持标量或标量数组（数组与真实 queryParams 一样取最后一个）",
    )
    headers: dict[str, Any] = Field(default_factory=dict, description="请求头（键名自动转小写）")
    body: Any = Field(default=None, description="请求体（JSON 对象/数组/标量，省略视为空对象）")

    @field_validator("method")
    @classmethod
    def _method_allowed(cls, value: str) -> str:
        method = str(value).upper()
        if method not in _ALLOWED_METHODS:
            raise ValueError(f"不支持的 HTTP 方法: {value!r}")
        return method

    @field_validator("path")
    @classmethod
    def _path_valid(cls, value: str) -> str:
        if not isinstance(value, str) or not value.startswith("/"):
            raise ValueError("path 必须是以 / 开头的字符串")
        return value


class PreviewValidationError(ValueError):
    """预演请求体字段无法规整成真实请求形态。"""


def _normalize_scalar_map(raw: dict[str, Any], label: str) -> dict[str, str]:
    """把预演输入中的 query/headers 规整成真实请求的形态：键字符串、值字符串。

    query 的数组值与 Starlette ``query_params[key]`` 一致（取最后一个）；
    headers 只允许标量。``sim_session_token`` 不出现在真实业务 query 中，剔除。
    """
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise PreviewValidationError(f"{label} 的键必须是字符串: {key!r}")
        if label == "query" and key == "sim_session_token":
            continue
        if isinstance(value, list):
            if label != "query" or not value:
                raise PreviewValidationError(f"{label}.{key} 不支持数组值")
            value = value[-1]
        if not isinstance(value, _SCALAR_TYPES):
            allowed = "标量（字符串/数字/布尔）或标量数组" if label == "query" \
                else "标量（字符串/数字/布尔）"
            raise PreviewValidationError(f"{label}.{key} 的值必须是{allowed}")
        out[key.lower() if label == "header" else key] = str(value)
    return out


def _bad_preview(detail: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": "invalid_preview_request",
                                                  "detail": detail})


@router.post("/{token}/preview")
async def preview_request(token: str, body: PreviewBody) -> JSONResponse:
    """基于当前会话快照预演一次业务请求：复用真实路由匹配、条件判断与模板渲染，
    但不写库、不推进状态、不递增 seq 计数器（外层 HTTP 状态码与真实请求一致）。"""
    try:
        query = _normalize_scalar_map(body.query, "query")
        headers = _normalize_scalar_map(body.headers, "header")
    except PreviewValidationError as exc:
        return _bad_preview(str(exc))

    parsed_body: Any = {} if body.body is None else body.body

    status_code, envelope = preview(
        token=token,
        method=body.method,
        path=body.path,
        query=query,
        headers=headers,
        body=parsed_body,
        default_scenario=settings.default_scenario,
        explicit_scenario=headers.get("sim-scenario"),
    )
    return JSONResponse(status_code=status_code, content=envelope)


class ResetBody(BaseModel):
    state: str | None = None
    context: dict | None = None


@router.post("/{token}/reset")
async def reset_session(token: str, body: ResetBody) -> JSONResponse:
    row = _session_row(token)
    if row is None:
        return JSONResponse(status_code=404, content={"error": "session_not_found", "token": token})
    scenario = store.get(row["scenario"])
    if scenario is None:
        return JSONResponse(status_code=410, content={"error": "scenario_unloaded"})

    state = body.state or scenario.initial_state
    if state not in scenario.states:
        return JSONResponse(status_code=422, content={"error": "unknown_state",
                                              "state": state, "states": scenario.states})
    context = body.context if body.context is not None else {}
    context.setdefault("_seq", {})

    with write_tx() as conn:
        conn.execute(
            "UPDATE sessions SET state = ?, context = ?, updated_at = datetime('now') WHERE token = ?",
            (state, dumps(context), token),
        )
        conn.execute("DELETE FROM events WHERE token = ?", (token,))
    return JSONResponse(_serialize(_session_row(token)))


@router.delete("/{token}")
async def delete_session(token: str) -> JSONResponse:
    with write_tx() as conn:
        cur = conn.execute("DELETE FROM events WHERE token = ?", (token,))
        cur2 = conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    if cur2.rowcount == 0:
        return JSONResponse(status_code=404, content={"error": "session_not_found", "token": token})
    return JSONResponse({"deleted": True, "token": token})
