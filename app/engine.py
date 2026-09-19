"""核心模拟引擎：在单个 IMMEDIATE 事务中完成匹配、渲染、状态推进。

真实请求（``simulate`` / ``_run_locked``）与预演请求（``preview``）共用
同一个纯计算函数 ``_plan``：它只读取会话快照并算出「会命中什么、会渲染出
什么、会把 state/context 改成什么」，不触碰数据库。真实路径随后在写事务中
落库；预演路径在只读事务中执行并整体回滚，因此模板里即使调用 ``seq()``
也不会改变计数器，下一次真实请求仍看到预演前的值。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl

from .db import dumps, read_tx, write_tx
from .matching import evaluate_rules_traced, match_path
from .models import Route, Rule, Scenario
from .renderer import RenderError, Renderer
from .store import store


class SimAbort(Exception):
    """以指定 HTTP 响应终止；commit_on_abort=True 时提交事务。"""

    def __init__(self, status_code: int, body: Any, *, commit_on_abort: bool = False,
                 route_id: str | None = None, trace: list[dict[str, Any]] | None = None):
        super().__init__(json.dumps(body, ensure_ascii=False))
        self.status_code = status_code
        self.body = body
        self.commit_on_abort = commit_on_abort
        # 预演用的诊断信息（真实路径忽略）
        self.route_id = route_id
        self.trace = trace


@dataclass
class SimResult:
    status_code: int
    headers: dict[str, str]
    body: Any
    delay_ms: int
    is_json: bool
    matched: dict[str, Any]


@dataclass
class Plan:
    """一次请求在当前快照上的完整计算结果（纯内存，无副作用）。"""
    scenario_name: str
    scenario: Scenario
    route: Route
    rule: Rule
    state: str
    context: dict[str, Any]
    method: str
    path: str
    query: dict[str, Any]
    headers: dict[str, str]
    body: Any
    path_params: dict[str, str]
    status_code: int
    resp_headers: dict[str, str]
    resp_body: Any
    is_json: bool
    next_state: str
    new_context: dict[str, Any]
    renderer: Renderer
    trace: list[dict[str, Any]]
    is_new_session: bool = False
    is_preview: bool = False
    route_candidates: list[Route] = field(default_factory=list)
    allowed_methods: list[str] = field(default_factory=list)


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
# 纯计算：匹配 -> 渲染 -> 计算状态/上下文差异（不读写数据库）
# ---------------------------------------------------------------------------

def _plan(*, scenario_name: str, scenario: Scenario, state: str, context: dict[str, Any],
          method: str, path: str, query: dict[str, Any], headers: dict[str, str],
          body: Any, path_params: dict[str, str], route: Route,
          is_new_session: bool = False, is_preview: bool = False) -> Plan:
    bag = {"state": state, "body": body, "query": query,
           "header": headers, "path": path_params}
    rule, trace = evaluate_rules_traced(route.rules, bag)

    if rule is None:
        raise SimAbort(409, {
            "error": "no_matching_rule",
            "scenario": scenario_name,
            "route": route.id,
            "state": state,
        }, commit_on_abort=not is_preview, route_id=route.id, trace=trace)

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
                             "rule": rule.name, "detail": str(exc)},
                        route_id=route.id, trace=trace) from exc

    if not isinstance(updates, dict):
        raise SimAbort(500, {"error": "render_error",
                             "detail": f"规则 {rule.name} 的 set 渲染结果必须是对象"},
                        route_id=route.id, trace=trace)

    new_context = dict(context)
    new_context.update(updates)
    new_context["_seq"] = renderer.counters

    next_state = state
    if rule.next_state is not None:
        next_state = renderer.render_template(rule.next_state).strip()
        if next_state not in scenario.states:
            raise SimAbort(500, {"error": "render_error",
                                 "detail": f"规则 {rule.name} 渲染后的状态 {next_state!r} 未声明"},
                            route_id=route.id, trace=trace)

    return Plan(
        scenario_name=scenario_name,
        scenario=scenario,
        route=route,
        rule=rule,
        state=state,
        context=context,
        method=method,
        path=path,
        query=query,
        headers=headers,
        body=body,
        path_params=path_params,
        status_code=status,
        resp_headers=resp_headers,
        resp_body=resp_body,
        is_json=is_json,
        next_state=next_state,
        new_context=new_context,
        renderer=renderer,
        trace=trace,
        is_new_session=is_new_session,
        is_preview=is_preview,
    )


# ---------------------------------------------------------------------------
# 真实业务请求
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

    # 2) 匹配路由（失败则回滚，不留下空会话）
    route, path_params = _match_route(scenario, method, path)

    # 3) 新会话落库（在渲染之前，确保“路径匹配成功即建会话”的既有语义）
    if is_new_session:
        try:
            conn.execute(
                "INSERT INTO sessions(token, scenario, state, context) VALUES (?, ?, ?, ?)",
                (token, scenario_name, state, dumps(context)),
            )
        except sqlite3.IntegrityError:
            # 并发首请求：另一事务已插入，转为锁冲突重试
            raise sqlite3.OperationalError("database is locked") from None

    # 4) 纯计算：选规则 -> 渲染 -> 推进状态（no_matching_rule 在此提交会话）
    plan = _plan(
        scenario_name=scenario_name, scenario=scenario, state=state, context=context,
        method=method, path=path, query=query, headers=headers, body=body,
        path_params=path_params, route=route, is_new_session=is_new_session,
    )

    # 5) 落库：更新会话 + 事件
    conn.execute(
        "UPDATE sessions SET state = ?, context = ?, updated_at = datetime('now') WHERE token = ?",
        (plan.next_state, dumps(plan.new_context), token),
    )
    conn.execute(
        """
        INSERT INTO events(token, scenario, method, path, route, rule,
                           from_state, to_state, status_code)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (token, scenario_name, method, path, route.id, plan.rule.name,
         state, plan.next_state, plan.status_code),
    )

    return _plan_to_result(plan)


def _plan_to_result(plan: Plan) -> SimResult:
    return SimResult(
        status_code=plan.status_code,
        headers=plan.resp_headers,
        body=plan.resp_body,
        delay_ms=plan.rule.delay_ms,
        is_json=plan.is_json,
        matched={
            "scenario": plan.scenario_name,
            "route": plan.route.id,
            "rule": plan.rule.name,
            "from_state": plan.state,
            "to_state": plan.next_state,
        },
    )


# ---------------------------------------------------------------------------
# 预演（preview）：只读快照事务 + 全程回滚，不产生任何持久化副作用
# ---------------------------------------------------------------------------

def _public_context(context: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in context.items() if k != "_seq"}


def _context_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """set 语义为覆盖式更新：只汇报新增/变更的键（_seq 单独展示）。"""
    diff: dict[str, Any] = {}
    for key, value in after.items():
        if key == "_seq":
            continue
        if key not in before or before[key] != value:
            diff[key] = value
    return diff


def _seq_diff(before: dict[str, Any], after: dict[str, Any],
              called: dict[str, int]) -> dict[str, dict[str, Any]]:
    """仅列出本次模板实际调用过的 seq 计数器的前后取值。"""
    out: dict[str, dict[str, Any]] = {}
    for name, returned in called.items():
        out[name] = {
            "before": before.get(name),
            "returned_by_template": returned,
            "would_be_after": after.get(name),
            "persisted": False,
        }
    return out


def preview(*, token: str, method: str, path: str, query: dict[str, Any],
            headers: dict[str, str], body: Any,
            default_scenario: str | None = None,
            explicit_scenario: str | None = None) -> tuple[int, dict[str, Any]]:
    """对既有会话快照执行一次不落库的预演。

    返回 ``(http_status, envelope)``：外层 HTTP 状态码与真实请求完全一致；
    成功或 ``no_matching_rule`` 时 envelope 含完整匹配/响应/差异/轨迹，
    其他失败（404/405/渲染错误等）envelope 即真实请求会返回的错误体。
    """
    request_echo = {"method": method, "path": path, "query": query,
                    "headers": headers, "body": body}

    with read_tx() as conn:
        row = conn.execute(
            "SELECT scenario, state, context FROM sessions WHERE token = ?",
            (token,),
        ).fetchone()

        if row is None:
            # 与 GET /__sessions/{token} 一致；不做懒创建（预演不得创建会话）
            return 404, {"error": "session_not_found", "token": token,
                         "request": request_echo}

        scenario_name = row["scenario"]
        scenario = store.get(scenario_name)
        if scenario is None:
            return 410, {"error": "scenario_unloaded", "scenario": scenario_name,
                         "request": request_echo}

        state = row["state"]
        context = json.loads(row["context"])

        try:
            route, path_params = _match_route(scenario, method, path)
            plan = _plan(
                scenario_name=scenario_name, scenario=scenario, state=state,
                context=context, method=method, path=path, query=query,
                headers=headers, body=body, path_params=path_params, route=route,
                is_preview=True,
            )
        except SimAbort as exc:
            envelope: dict[str, Any] = dict(exc.body)
            envelope["request"] = request_echo
            envelope["snapshot"] = {
                "scenario": scenario_name, "state": state,
                "context": _public_context(context),
            }
            if exc.route_id is not None:
                envelope["trace"] = {
                    "scenario": scenario_name,
                    "route": exc.route_id,
                    "rules": exc.trace or [],
                }
            return exc.status_code, envelope

        before_seq = dict(context.get("_seq") or {})
        return plan.status_code, {
            "outcome": "matched",
            "request": request_echo,
            "snapshot": {
                "token": token,
                "scenario": scenario_name,
                "state": state,
                "context": _public_context(context),
            },
            "matched": {
                "scenario": plan.scenario_name,
                "route": plan.route.id,
                "rule": plan.rule.name,
                "path_params": plan.path_params,
            },
            "response": {
                "status": plan.status_code,
                "headers": plan.resp_headers,
                "body": plan.resp_body,
                "delay_ms": plan.rule.delay_ms,
            },
            "state_diff": {
                "from": plan.state,
                "to": plan.next_state,
                "changed": plan.state != plan.next_state,
            },
            "context_diff": {
                "set": _context_diff(plan.context, plan.new_context),
                "before": _public_context(plan.context),
                "would_be_after": _public_context(plan.new_context),
            },
            "seq": _seq_diff(before_seq, plan.renderer.counters,
                             plan.renderer.seq_calls),
            "trace": {
                "scenario": plan.scenario_name,
                "route": plan.route.id,
                "rules": plan.trace,
            },
            "persisted": False,
        }
