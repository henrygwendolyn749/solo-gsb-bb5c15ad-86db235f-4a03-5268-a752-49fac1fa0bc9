"""FastAPI 入口：启动初始化、场景自动载入、全路径模拟转发。"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .config import settings
from .db import get_conn, init_db
from .engine import SimAbort, SimResult, simulate
from .routers import scenarios, sessions
from .scenario import parse_scenario
from .store import store

logger = logging.getLogger("simulator")


def _autoload_scenarios() -> None:
    """从目录批量载入 *.yaml/*.yml（文件名即场景名）。"""
    directory = settings.autoload_dir
    if not directory or not os.path.isdir(directory):
        return
    loaded = 0
    conn = get_conn()
    for filename in sorted(os.listdir(directory)):
        if not (filename.endswith(".yaml") or filename.endswith(".yml")):
            continue
        name = filename.rsplit(".", 1)[0]
        path = os.path.join(directory, filename)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        scenario = parse_scenario(text)  # 校验，失败则启动失败、暴露问题
        conn.execute(
            """
            INSERT INTO scenarios(name, definition, created_at, updated_at)
            VALUES (?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(name) DO UPDATE SET
                definition = excluded.definition,
                updated_at = datetime('now')
            """,
            (scenario.name, text),
        )
        loaded += 1
    conn.commit()
    if loaded:
        logger.info("自动载入 %d 个场景: %s", loaded, directory)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    store.load_all_from_db()
    _autoload_scenarios()
    store.load_all_from_db()
    logger.info("就绪，已加载场景: %s", store.names() or "<无>")
    yield


app = FastAPI(
    title="有状态 API 场景模拟器",
    description="提交 YAML 场景，按会话令牌隔离状态，单事务匹配/渲染/迁移。",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(scenarios.router)
app.include_router(sessions.router)


@app.get("/__health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok", "scenarios": store.names()}


def _build_response(result: SimResult):
    headers = dict(result.headers)
    if result.body is None:
        media_type = headers.get("Content-Type") or headers.get("content-type")
        response = Response(status_code=result.status_code, headers=headers,
                            media_type=media_type)
    elif result.is_json:
        response = JSONResponse(content=result.body, status_code=result.status_code,
                                headers=headers)
    else:
        media_type = headers.pop("Content-Type", headers.pop("content-type", "text/plain; charset=utf-8"))
        text = result.body if isinstance(result.body, str) else str(result.body)
        response = Response(content=text, status_code=result.status_code,
                            headers=headers, media_type=media_type)
    response.headers["X-Sim-Matched"] = (
        f"{result.matched['scenario']}:{result.matched['route']}:{result.matched['rule']}"
    )
    response.headers["X-Sim-State"] = (
        f"{result.matched['from_state']}->{result.matched['to_state']}"
    )
    return response


# 注册在管理路由之后：除上述显式路径外的所有请求都按模拟流量处理
@app.api_route(
    "/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def catch_all(request: Request, rest: str = ""):
    try:
        result = await simulate(request, default_scenario=settings.default_scenario)
    except SimAbort as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body)

    if result.delay_ms:
        await asyncio.sleep(result.delay_ms / 1000.0)
    return _build_response(result)
