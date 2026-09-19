"""会话管理接口：创建 / 查询状态 / 重置 / 事件历史。"""
from __future__ import annotations

import json
import secrets
import sqlite3

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..db import dumps, get_conn, write_tx
from ..store import store

router = APIRouter(prefix="/__sessions", tags=["sessions"])


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
