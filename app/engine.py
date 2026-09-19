"""核心模拟引擎：在单个 IMMEDIATE 事务中完成匹配、渲染、状态推进。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

from .db import dumps, write_tx
from .matching import evaluate_condition, get_condition_value, match_path
from .models import Route, Rule, Scenario
from .renderer import RenderError, Renderer
from .store import store


class SimAbort(Exception):
    """以指定 HTTP 响应终止；commit_on_abort=True 时提交事务。"""

    def __init__(self, status_code: int, body: Any, *, commit_on_abort: bool = False):
        super().__init__(json.dumps(body, ensure_ascii=False))
        self.status_code = status_code
        self.body = body
        self.commit_on_abort = commit_on_abort


@dataclass
class SimResult:
    status_code: int
    headers: dict[str, str]
    body: Any
    delay_ms: int
    is_json: bool
    matched: dict[str, Any]


# ---------------------------------------------------------------------------
# 请求解析
# ---------------------------------------------------------------------------

async def _read_body(request) -> Any:
    raw_bytes = await request.body()
    raw = raw_bytes.decode("utf-8") if raw_bytes else ""
    if not raw:
        return {}
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type or raw.lstrip().startswith(("{", "[")):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SimAbort(400, {"error": "invalid_json", "detail": str(exc)}) from exc
    if "application/x-www-form-urlencoded" in content_type:
        return dict(parse_qsl(raw, keep_blank_values=True))
    if "yaml" in content_type:
        import yaml
        try:
            return yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            raise SimAbort(400, {"error": "invalid_yaml", "detail": str(exc)}) from exc
    return {"_raw": raw}


def _select_scenario(explicit: str | None, default_scenario: str | None) -> Scenario:
    if explicit:
        scenario = store.get(explicit)
        if scenario is None:
            raise SimAbort(404, {"error": "scenario_not_found", "scenario": explicit})
        return scenario
    if default_scenario:
        scenario = store.get(default_scenario)
        if scenario is None:
            raise SimAbort(400, {"error": "no_scenario_bound",
                                 "detail": "会话未指定场景且默认场景不存在"})
        return scenario
    names = store.names()
    if len(names) == 1:
        return store.get(names[0])  # type: ignore[return-value]
    if not names:
        raise SimAbort(400, {"error": "no_scenario_loaded",
                             "detail": "请先通过 /__scenarios 载入场景，或创建会话时指定 scenario"})
    raise SimAbort(409, {"error": "ambiguous_scenario",
                         "detail": "存在多个场景，请在创建会话时指定 scenario",
                         "scenarios": names})


def _match_route(scenario: Scenario, method: str, path: str) -> tuple[Route, dict[str, str]]:
    candidates: list[tuple[Route, dict[str, str]]] = []
    for route in scenario.routes:
        params = match_path(route.path_re, path)
        if params is not None:
            candidates.append((route, params))
    if not candidates:
        raise SimAbort(404, {"error": "no_route", "path": path})

    method_matches = [c for c in candidates if method in c[0].methods]
    if not method_matches:
        allowed = sorted({m for r, _ in candidates for m in r.methods})
        raise SimAbort(405, {"error": "method_not_allowed", "allowed": allowed})

    return method_matches[0]


def _evaluate_rules(rule_list: list[Rule], bag: dict[str, Any]) -> Rule | None:
    for rule in rule_list:  # 已按 priority 降序排序
        for cond in rule.conditions:
            present, actual = get_condition_value(cond.source, cond.path, bag)
            if not evaluate_condition(cond, actual, present):
                break
        else:
            return rule
    return None


def _render_response(rule: Rule, renderer: Renderer) -> tuple[int, dict[str, str], Any, bool]:
    spec = rule.response
    status_value = renderer.render_value(spec.get("status", 200))
    try:
        status = int(str(status_value).strip())
    except (TypeError, ValueError) as exc:
        raise SimAbort(500, {"error": "bad_rendered_status", "value": status_value}) from exc
    if not 100 <= status <= 599:
        raise SimAbort(500, {"error": "bad_rendered_status", "value": status})

    headers = {str(k): str(renderer.render_value(v))
               for k, v in (spec.get("headers") or {}).items()}

    if "body" not in spec:
        return status, headers, None, False

    body = renderer.render_value(spec["body"])
    explicit_json = any(k.lower() == "content-type" and "json" in v.lower()
                        for k, v in headers.items())
    is_json = explicit_json or isinstance(body, (dict, list))
    return status, headers, body, is_json


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

async def simulate(request, default_scenario: str | None = None) -> SimResult:
    token = (request.headers.get("sim-session-token")
             or request.query_params.get("sim_session_token"))
    if not token:
        raise SimAbort(400, {"error": "missing_session_token",
                             "detail": "请通过 sim-session-token 请求头（或 sim_session_token 查询参数）提供会话令牌"})

    method = request.method
    path = request.url.path
    query = {k: v for k, v in request.query_params.multi_items()
             if k != "sim_session_token"}
    headers = {k.lower(): v for k, v in request.headers.items()}
    body = await _read_body(request)
    # 新会话首次请求时可用 sim-scenario 头指定场景；已有会话忽略
    explicit_scenario = request.headers.get("sim-scenario")

    with write_tx() as conn:
        return _run_locked(conn, token, method, path, query, headers, body,
                           default_scenario, explicit_scenario)


def _run_locked(conn, token, method, path, query, headers, body,
                default_scenario, explicit_scenario) -> SimResult:
    # 1) 查找会话与场景
    row = conn.execute(
        "SELECT scenario, state, context FROM sessions WHERE token = ?",
        (token,),
    ).fetchone()

    if row is not None:
        scenario_name = row["scenario"]
        scenario = store.get(scenario_name)
        if scenario is None:
            raise SimAbort(410, {"error": "scenario_unloaded", "scenario": scenario_name})
        state = row["state"]
        context = json.loads(row["context"])
        is_new_session = False
    else:
        scenario = _select_scenario(explicit_scenario, default_scenario)
        scenario_name = scenario.name
        state = scenario.initial_state
        context = {}
        is_new_session = True

    # 2) 匹配路由（不存在则回滚，不留下空会话）
    route, path_params = _match_route(scenario, method, path)

    # 3) 新会话落库
    if is_new_session:
        try:
            conn.execute(
                "INSERT INTO sessions(token, scenario, state, context) VALUES (?, ?, ?, ?)",
                (token, scenario_name, state, dumps(context)),
            )
        except sqlite3.IntegrityError:
            # 并发首请求：另一事务已插入，转为锁冲突重试
            raise sqlite3.OperationalError("database is locked") from None

    # 4) 选择最高优先级匹配规则
    bag = {"state": state, "body": body, "query": query,
           "header": headers, "path": path_params}
    rule = _evaluate_rules(route.rules, bag)

    if rule is None:
        raise SimAbort(409, {
            "error": "no_matching_rule",
            "scenario": scenario_name,
            "route": route.id,
            "state": state,
        }, commit_on_abort=True)

    # 5) 渲染响应 + 推进状态（同一事务内）
    render_ctx = dict(context)
    render_ctx.update({
        "state": state,
        "body": body,
        "query": query,
        "header": headers,
        "path": path_params,
        "request": {"method": method, "path": path, "route": route.id},
        "context": dict(context),
    })
    renderer = Renderer(render_ctx)

    try:
        status, resp_headers, resp_body, is_json = _render_response(rule, renderer)
        updates = renderer.render_value(rule.set) if rule.set else {}
    except RenderError as exc:
        raise SimAbort(500, {"error": "render_error",
                             "rule": rule.name, "detail": str(exc)}) from exc

    if not isinstance(updates, dict):
        raise SimAbort(500, {"error": "render_error",
                             "detail": f"规则 {rule.name} 的 set 渲染结果必须是对象"})

    new_context = dict(context)
    new_context.update(updates)
    new_context["_seq"] = renderer.counters

    next_state = state
    if rule.next_state is not None:
        next_state = renderer.render_template(rule.next_state).strip()
        if next_state not in scenario.states:
            raise SimAbort(500, {"error": "render_error",
                                 "detail": f"规则 {rule.name} 渲染后的状态 {next_state!r} 未声明"})

    conn.execute(
        "UPDATE sessions SET state = ?, context = ?, updated_at = datetime('now') WHERE token = ?",
        (next_state, dumps(new_context), token),
    )
    conn.execute(
        """
        INSERT INTO events(token, scenario, method, path, route, rule,
                           from_state, to_state, status_code)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (token, scenario_name, method, path, route.id, rule.name,
         state, next_state, status),
    )

    return SimResult(
        status_code=status,
        headers=resp_headers,
        body=resp_body,
        delay_ms=rule.delay_ms,
        is_json=is_json,
        matched={
            "scenario": scenario_name,
            "route": route.id,
            "rule": rule.name,
            "from_state": state,
            "to_state": next_state,
        },
    )
