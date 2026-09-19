# 有状态 API 场景模拟器（Stateful API Scenario Simulator）

用于联调的有状态 API Mock 服务：提交一份包含**路由、请求匹配条件、响应模板、状态迁移**的
YAML 场景，服务按**会话令牌**隔离每个调用方的当前状态，并在**单个 SQLite 写事务**中
完成「选择最高优先级匹配 → 渲染响应 → 推进状态」，保证同一会话的并发请求不会重复触发同一条迁移。

- 技术栈：Python 3.12 · FastAPI · SQLite（WAL）· Jinja2 沙箱模板 · PyYAML
- 容器内启动即自动完成依赖安装与数据库初始化，并自动加载 `scenarios/` 下的示例场景

---

## 一条命令启动

```bash
docker compose up --build -d
```

服务地址：<http://localhost:8080>

| 用途 | 地址 |
| --- | --- |
| 健康检查 | <http://localhost:8080/__health> |
| Swagger 文档 | <http://localhost:8080/docs> |
| 场景管理 | `http://localhost:8080/__scenarios` |
| 会话状态 | `http://localhost:8080/__sessions/{token}` |
| 模拟业务流量 | 除上述管理前缀外的任意路径，如 `POST http://localhost:8080/orders` |

停止：`docker compose down`（数据保留在命名卷 `simulator-data` 中；加 `-v` 一并清除）。

不使用容器时：

```bash
pip install -r requirements.txt
SIM_DB_PATH=./data/simulator.db SIM_SCENARIOS_DIR=./scenarios \
  uvicorn app.main:app --host 0.0.0.0 --port 8080
```

---

## 快速体验

镜像自带 `scenarios/order-flow.yaml`（下单 → 支付 → 发货 → 收货，含取消分支）。

```bash
BASE=http://localhost:8080

# 1) 用令牌 t1 发起第一次业务请求（会话自动创建，初始状态 init）
curl -s -X POST $BASE/orders \
  -H 'sim-session-token: t1' -H 'Content-Type: application/json' \
  -d '{"sku":"A-1","quantity":2}'
# -> 201, 状态 init -> created

# 2) 支付（created -> paid）
curl -s -X POST $BASE/orders/42/pay \
  -H 'sim-session-token: t1' -H 'Content-Type: application/json' \
  -d '{"channel":"alipay"}'

# 3) 查询该会话当前状态与上下文
curl -s $BASE/__sessions/t1

# 4) 查询请求/迁移事件历史
curl -s "$BASE/__sessions/t1/events"
```

每个模拟响应还带有调试响应头：

- `X-Sim-Matched: order-flow:pay-order:pay-created`（场景:路由:规则）
- `X-Sim-State: created->paid`（迁移前后状态）

---

## 场景 YAML 写法

```yaml
name: order-flow                 # 必填，场景名（字母数字 . _ -）
description: 可选描述
initial_state: init              # 新会话的初始状态
states: [init, created, paid, shipped, completed, cancelled]

routes:
  - id: pay-order                # 路由 id，仅用于标识
    path: /orders/{order_id}/pay # 支持 {path_param} 路径参数
    methods: [POST]              # 默认 [GET, POST]
    rules:                       # 同一路由下按 priority 从高到低匹配
      - name: pay-created
        priority: 100
        when:                    # 所有条件为 AND；全部满足即命中
          - source: state        # state: 当前会话状态
            op: eq
            value: created
          - source: body         # body / query / header / path / state
            path: channel        # 点路径，支持 a.b.0 与 a[0].b
            op: in
            value: [alipay, wechat, card]
        response:
          status: 200            # 也支持 Jinja 模板，如 "{{ 200 if body.ok else 400 }}"
          headers:
            Content-Type: application/json
          body:                  # dict/list 自动按 JSON 返回；字符串按文本返回
            order_id: "{{ path.order_id }}"
            trace: "{{ uuid4() }}"
            paid_at: "{{ now('%Y-%m-%dT%H:%M:%SZ') }}"
        set:                     # 命中后写入会话上下文（持久化）
          pay_channel: "{{ body.channel }}"
        next_state: paid         # 命中后迁移；省略则状态不变
        delay_ms: 0              # 可选：响应前延迟（模拟慢接口）

      - name: double-pay-rejected # 低优先级兜底规则
        priority: 50
        when:
          - {source: state, op: in, value: [paid, shipped, completed]}
        response:
          status: 409
          body: {error: already_paid}
```

### 匹配条件（when）

| 字段 | 说明 |
| --- | --- |
| `source` | `body`（默认，JSON 解析）、`query`、`header`（键名小写）、`path`（路径参数）、`state` |
| `path` | 点路径：`a.b.0` / `a[0].b`；`source=state` 时忽略 |
| `op` | `eq` `ne` `gt` `gte` `lt` `lte` `in` `contains` `starts_with` `ends_with` `regex` `exists` `truthy` |
| `value` | 比较右值；`eq/ne/gt...` 会尽量按数字比较（如 `"2"` 与 `2` 相等） |

匹配语义：规则按 `priority` 降序（相同按定义顺序），**取第一条全部条件满足的规则**；
没有规则命中时返回 `409 no_matching_rule`（不会推进状态）；路径无匹配返回 `404`，
方法不支持返回 `405`。

### 响应模板（Jinja2 沙箱）

- 整个值写成单个 `"{{ expr }}"` 时保留原生类型（数字、布尔、列表、对象）；
  嵌在字符串中则渲染为文本。
- 可用变量：`state`、`body`、`query`、`header`、`path`、`context`（已持久化的上下文）、
  `request`（`method`/`path`/`route`）。
- 内置函数：`uuid4()`、`now(fmt)`、`random_str(n, prefix='')`、`randint(a,b)`、
  `choice(list)`、`seq(name, start=1)`（会话内自增计数器，跨请求持久化）。
- 过滤器：Jinja2 标准过滤器（如 `body.x | default('v')`）。
- 响应体 `body` 为 dict/list 时按 JSON 返回；为字符串时配合 `Content-Type` 头
  可返回 HTML/纯文本等。

### 校验规则

载入时会校验：场景名合法、`initial_state` 在 `states` 内、路径/方法合法、
条件 `source/op` 合法、`next_state` 已声明、状态码范围、模板语法正确、
路由 id 与「路径+方法」组合不重复、业务路径不得占用 `/__*`、`/docs` 等管理前缀。

完整示例见 [`scenarios/order-flow.yaml`](scenarios/order-flow.yaml)。

---

## 管理接口

### 场景

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/__scenarios/validate` | 只校验不入库。body 为 YAML 文本（`Content-Type: application/yaml`）或 JSON |
| GET | `/__scenarios` | 列出全部场景及解析后的结构 |
| GET | `/__scenarios/{name}` | 查看单个场景摘要 |
| GET | `/__scenarios/{name}/yaml` | 取回原始 YAML |
| PUT | `/__scenarios/{name}` | **校验并载入**（新增或替换），替换对新会话立即生效 |
| DELETE | `/__scenarios/{name}` | 删除；仍有会话引用时返回 409，加 `?force=true` 级联删除 |

```bash
# 校验
curl -s -X POST $BASE/__scenarios/validate \
  -H 'Content-Type: application/yaml' --data-binary @my-scenario.yaml

# 载入（文件名/场景名任意，以 URL 中的 name 为准）
curl -s -X PUT $BASE/__scenarios/my-scenario \
  -H 'Content-Type: application/yaml' --data-binary @my-scenario.yaml
```

### 会话

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/__sessions` | 显式创建会话：`{"token":"t1","scenario":"order-flow","state":"created","context":{}}`，字段均可省略 |
| GET | `/__sessions/{token}` | **会话状态查询**：当前状态、上下文、可用状态、路由列表 |
| GET | `/__sessions/{token}/events?limit=50` | 最近的匹配与状态迁移历史（新→旧） |
| POST | `/__sessions/{token}/reset` | 重置到初始（或指定）状态并清空事件 |
| DELETE | `/__sessions/{token}` | 删除会话及其事件 |

会话也可以**懒创建**：业务请求只需携带令牌；首个匹配到路由的请求自动建会话。
当已载入多个场景时，懒创建需通过请求头 `sim-scenario: <name>` 指定场景
（或设置环境变量 `SIM_DEFAULT_SCENARIO`）。

### 业务请求约定

- 会话令牌：请求头 `sim-session-token: <token>`（也支持 `?sim_session_token=`）。
- 可选请求头 `sim-scenario: <name>`：仅新会话首次请求时用于绑定场景。
- 请求体：`application/json` 按 JSON 解析；`application/x-www-form-urlencoded`
  按表单解析；YAML 的 content-type 按 YAML 解析；其他文本保存为 `body._raw`。

---

## 并发安全：为什么迁移不会重复触发

所有会读写会话的业务请求都在同一个 `BEGIN IMMEDIATE` 事务中执行：

1. 立即获取 SQLite 写锁（同库同刻只有一个写事务）；
2. 读取会话行（`state`/`context`）；
3. 匹配路由与最高优先级规则；
4. 渲染响应、计算 `set` 与 `next_state`；
5. 更新会话并写入一条 `events` 记录，然后提交。

拿不到锁时按 `SIM_BUSY_TIMEOUT_MS` 等待并在应用层重试（`SIM_DB_RETRIES` 次）。
因此同一会话的并发请求会被**串行化**，每个请求看到的都是前一个请求提交后的新状态：
例如 20 个并发支付请求中恰好 1 个得到 200 并完成 `created -> paid`，
其余 19 个得到 409（已内置压测验证，见下方"压测"说明）。

> 部署限制：SQLite 的写锁是**进程内文件级**的，因此容器固定 `--workers 1`。
> 单 worker + 异步框架足以支撑联调量级的并发；会话状态使用线程局部连接，
> 同步事务段很短，不阻塞事件循环。

---

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SIM_PORT`（仅 compose） | `8080` | 宿主机映射端口，改后访问 `http://localhost:<端口>` |
| `SIM_DB_PATH` | `/data/simulator.db` | SQLite 文件路径（compose 已挂卷持久化） |
| `SIM_SCENARIOS_DIR` | `/app/scenarios` | 启动时自动加载的场景目录（`*.yaml`/`*.yml`，文件名即场景名） |
| `SIM_DEFAULT_SCENARIO` | 空 | 多场景时未显式指定场景的默认场景名 |
| `SIM_BUSY_TIMEOUT_MS` | `5000` | 写锁等待毫秒 |
| `SIM_DB_RETRIES` | `20` | 锁冲突时的应用侧重试次数（每次 20ms 起退避） |
| `SIM_LOG_LEVEL` | `info` | 日志级别 |

自定义场景目录（改 YAML 无需重新构建镜像）：

```bash
# docker-compose.yml 中取消注释挂载行，或：
SIM_SCENARIOS_DIR=/app/scenarios docker compose run --rm \
  -v "$PWD/my-scenarios:/app/scenarios" -p 8080:8080 api-simulator
```

---

## 项目结构

```
.
├── app/
│   ├── main.py            # FastAPI 入口、生命周期初始化、全路径转发
│   ├── config.py          # 环境变量配置
│   ├── db.py              # 连接/WAL/建表/IMMEDIATE 事务与锁重试
│   ├── scenario.py        # YAML 解析与校验
│   ├── models.py          # 场景/路由/规则数据模型
│   ├── matching.py        # 路径模式、点路径取值、条件运算符
│   ├── renderer.py        # Jinja2 沙箱渲染与模板函数
│   ├── engine.py          # 核心：单事务匹配→渲染→迁移
│   ├── store.py           # 已编译场景缓存
│   └── routers/
│       ├── scenarios.py   # 场景校验/载入/查询/删除
│       └── sessions.py    # 会话创建/状态查询/重置/事件
├── scenarios/
│   └── order-flow.yaml    # 示例场景（随启动自动加载）
├── requirements.txt
├── Dockerfile             # 依赖安装 + DB 初始化随启动完成
├── docker-compose.yml
└── README.md
```

数据库表：`scenarios`（场景定义）、`sessions`（令牌 → 场景/状态/上下文）、
`events`（每次命中的路由、规则、前后状态与状态码）。

---

## 并发自测（可选）

```bash
BASE=http://localhost:8080
curl -s -X POST $BASE/__sessions -H 'Content-Type: application/json' \
  -d '{"token":"ct","state":"created"}' >/dev/null
seq 1 20 | xargs -P 20 -I{} curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST $BASE/orders/1/pay \
  -H 'sim-session-token: ct' -H 'Content-Type: application/json' \
  -d '{"channel":"card"}' | sort | uniq -c
# 期望: 1 个 200，19 个 409
curl -s "$BASE/__sessions/ct/events"   # 事件中恰好 1 次 created -> paid
```
