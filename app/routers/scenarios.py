"""管理接口：场景校验 / 载入 / 查询 / 删除。"""
from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from ..db import get_conn
from ..scenario import ScenarioError, parse_scenario
from ..store import store

router = APIRouter(prefix="/__scenarios", tags=["scenarios"])


async def _read_definition(request: Request) -> str:
    raw = (await request.body()).decode("utf-8")
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        import json
        data = json.loads(raw)
        if isinstance(data, dict) and "definition" in data:
            if not isinstance(data["definition"], str):
                return yaml.safe_dump(data["definition"], allow_unicode=True, sort_keys=False)
            return data["definition"]
        # 整个 JSON 就当作场景定义
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return raw


def _summary(scenario) -> dict[str, Any]:
    return {
        "name": scenario.name,
        "description": scenario.description,
        "initial_state": scenario.initial_state,
        "states": scenario.states,
        "routes": [
            {
                "id": r.id,
                "path": r.path,
                "methods": r.methods,
                "rules": [
                    {"name": rule.name, "priority": rule.priority,
                     "next_state": rule.next_state}
                    for rule in r.rules
                ],
            }
            for r in scenario.routes
        ],
    }


@router.post("/validate")
async def validate_scenario(request: Request) -> JSONResponse:
    """仅校验不入库。"""
    text = await _read_definition(request)
    try:
        scenario = parse_scenario(text)
    except ScenarioError as exc:
        return JSONResponse(status_code=422, content={"valid": False, "error": str(exc)})
    except yaml.YAMLError as exc:
        return JSONResponse(status_code=422, content={"valid": False, "error": f"YAML 解析失败: {exc}"})
    return JSONResponse({"valid": True, "scenario": _summary(scenario)})


@router.get("")
async def list_scenarios() -> JSONResponse:
    conn = get_conn()
    rows = conn.execute(
        "SELECT name, created_at, updated_at FROM scenarios ORDER BY name"
    ).fetchall()
    return JSONResponse({
        "scenarios": [
            {"name": r["name"], "created_at": r["created_at"],
             "updated_at": r["updated_at"], **_summary(store.get(r["name"]))}
            for r in rows
        ]
    })


@router.get("/{name}")
async def get_scenario(name: str) -> JSONResponse:
    conn = get_conn()
    row = conn.execute(
        "SELECT name, definition, created_at, updated_at FROM scenarios WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return JSONResponse(status_code=404, content={"error": "scenario_not_found", "name": name})
    return JSONResponse({
        "name": row["name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "scenario": _summary(store.get(name)),
    })


@router.get("/{name}/yaml", response_class=PlainTextResponse)
async def get_scenario_yaml(name: str) -> PlainTextResponse:
    conn = get_conn()
    row = conn.execute("SELECT definition FROM scenarios WHERE name = ?", (name,)).fetchone()
    if row is None:
        return PlainTextResponse(
            f'{{"error":"scenario_not_found","name":"{name}"}}',
            status_code=404,
            media_type="application/json",
        )
    return PlainTextResponse(row["definition"], media_type="application/yaml")


@router.put("/{name}")
async def upsert_scenario(name: str, request: Request) -> JSONResponse:
    """校验并载入（新增或替换）场景。"""
    text = await _read_definition(request)
    existed = store.get(name) is not None
    try:
        scenario = store.upsert(name, text)
    except ScenarioError as exc:
        return JSONResponse(status_code=422, content={"valid": False, "error": str(exc)})
    except yaml.YAMLError as exc:
        return JSONResponse(status_code=422, content={"valid": False, "error": f"YAML 解析失败: {exc}"})
    return JSONResponse({
        "loaded": True,
        "action": "replaced" if existed else "created",
        "scenario": _summary(scenario),
    })


@router.delete("/{name}")
async def delete_scenario(name: str, request: Request) -> JSONResponse:
    force = request.query_params.get("force") in {"1", "true", "yes"}
    conn = get_conn()
    sessions = conn.execute(
        "SELECT COUNT(*) AS c FROM sessions WHERE scenario = ?", (name,)
    ).fetchone()["c"]
    if sessions and not force:
        return JSONResponse(status_code=409, content={
            "error": "scenario_in_use",
            "sessions": sessions,
            "detail": "仍有会话引用该场景；确认级联删除请加 ?force=true",
        })
    if not store.get(name):
        return JSONResponse(status_code=404, content={"error": "scenario_not_found", "name": name})
    if force:
        conn.execute("DELETE FROM events WHERE scenario = ?", (name,))
        conn.execute("DELETE FROM sessions WHERE scenario = ?", (name,))
        conn.commit()
    store.delete(name)
    return JSONResponse({"deleted": True, "name": name})
