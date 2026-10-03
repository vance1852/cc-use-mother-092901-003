# 执行差异化收费公路政策协同基础服务

本项目提供综合交通运输业务共享的服务端基础能力：在运营机构、交通节点、操作者和结构化参考资料登记之上，
新建了完整的**收费政策执行与清算服务**，支持节假日、夜间货运、民生车辆等差异化费率，并保证计费可解释、
分录可重放、退款/追缴金额守恒、争议只冻结相关分录。

## 目录

- `src/transport_coordination/`
  - 基础能力：领域模型、SQLite 存储、权限服务、哈希串联审计、HTTP 路由和离线验收；
  - `policy_engine.py`：费率/封顶/豁免/资格四类规则的纯函数确定性求值引擎（整数分，无浮点）；
  - `billing.py`：政策版本发布、行程事件、不可变计费事实、冲回/调整分录、争议冻结与历史重放；
  - `billing_acceptance.py`：收费清算端到端离线验收。
- `tests/`：基础规则、事务边界、接口路由、计费引擎与端到端验收测试。

## 核心规则

### 政策（不可变版本）

- 四类政策：`rate`（百分折扣）、`cap`（金额封顶）、`exemption`（全免）、`eligibility`（授予资格标签）；
- 每条政策带生效区间 `effective_from/effective_to`、优先级 `priority`（数字越小越优先）；
- 同一 `policy_id` 重复发布产生自增版本，历史版本永不修改；撤回打时间戳，**只影响撤回后才计费、
  尚未结算的行程**，不追溯已固化的计费事实。
- 求值顺序：资格规则先按优先级授予标签，随后豁免、费率、封顶按优先级依次作用；
  每条规则无论是否命中都写入计费轨迹，可完整解释"优惠顺序"。

### 行程事件与计费事实（只追加、可重放）

- 事件类型：`entry`（入口）、`exit`（出口）、`supplemental_exit`（出口补录）、
  `detour`（封路绕行路径更正）、`late_notice`（迟到通知）；
- 入口已登记但缺少可计费出口/路径时进入 `pending_evidence`（待补证），补录后出账；
- 每次路径更正生成**新版本的不可变计费事实**，并以"旧收费分录 + 金额取反的冲回分录 + 新收费分录"
  代替原地改写，各版本金额与当时政策快照一并固化；
- 普通出口重复上报不重计费，更正路径必须显式使用出口补录/绕行事件。

### 关账、调整与守恒

- 已结算（关账）收入不可变：迟到事件只登记并标记 `has_late_event`，不产生新金额，
  退款/追缴也被拒绝（需要先在新的结算期走专门调整流程）；
- 退款、追缴只能发生在结算前，按各运营方当前应收净额以最大余数法成比例分摊（整数分严格守恒）；
- 每次计费后校验分录合计等于事实金额，不守恒则整笔事务回滚。

### 争议冻结

- 开启争议只冻结该行程的分录：冻结期间该行程不能结算、不能调整、不能再生成计费版本；
- 其他运营方与其他行程完全不受影响，争议解决后分录解冻。

### 解释与重放

- `GET /trips/{id}/explain`：说明一次通行为什么得到当前金额（逐步优惠轨迹、各版本、
  每家运营方承担的收费/冲回/退款/追缴/净额）；
- `GET /trips/{id}/replay?as_of=...`：按历史观察时点重放，只使用该时点之前已记录的事件和
  当时已发布且未撤回的政策重新求值；
- `GET /reconciliation?period_id=...`：按结算期汇总每家运营方应收、冲回、退款、追缴、净额与冻结额。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/segments` | 登记收费路段（归属运营方、基础费，单位：分） |
| POST | `/policies` | 发布政策新版本 |
| POST | `/policies/withdraw` | 撤回指定政策版本（缺省最新版） |
| GET | `/policies` | 列出政策版本（`include_withdrawn=false` 隐藏已撤回） |
| POST | `/trip-events` | 登记入口/出口/补录/绕行/迟到事件 |
| GET | `/trips/{id}/explain` | 解释当前金额、优惠顺序与运营方分账 |
| GET | `/trips/{id}/replay?as_of=` | 按历史时点重放 |
| POST | `/adjustments` | 登记退款 `refund` / 追缴 `recovery` |
| POST | `/disputes` / `/disputes/resolve` | 开启争议（冻结相关分录）/ 解决 |
| POST | `/settlement-periods` / `/settlement-periods/close` | 建立/关账结算期 |
| POST | `/settlements` | 把行程结算进结算期 |
| GET | `/reconciliation?period_id=` | 运营方分账对账汇总 |

所有写接口要求 `X-Actor-Id` 与 `request_id`（幂等键）；政策发布/撤回、结算期与关账仅 `admin`，
退款/追缴允许 `admin`、`reviewer`，事件登记允许 `admin`、`operator`、`reviewer`。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

基础服务：

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
```

收费政策执行与清算（差异化政策、待补证、绕行重计费、守恒分账、关账不可变、撤回范围、
争议冻结与历史重放）：

```bash
PYTHONPATH=src python3 -m transport_coordination.billing_acceptance
```

成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的
业务状态、计费事实、分录和审计历史继续保留。
