# 执行差异化收费公路政策协同基础服务

本项目在综合交通运输共享服务端基础能力（运营机构、操作者、角色权限、请求幂等、SQLite 事务、哈希串联审计）之上，新建了完整的**收费政策执行与清算服务**。政策人员可发布带生效区间与优先级的费率、折扣、封顶、豁免及车辆资格规则；行程按经过的路段版本、入口证据与出口事件生成不可重复的计费事实；缺失出口进入待补证；迟到事件不得改写已关账收入；政策撤回只影响尚未结算的行程；退款、追缴与运营方分账采用复式分录保持金额守恒；争议只冻结相关分录；业务 API 可解释一次通行为什么得到当前金额、每家运营方承担多少调整，并能按历史时点重放当时有效的政策与清分结果。

## 目录

- `src/transport_coordination/`
  - 基础域：`models.py`、`domain.py`、`service.py`、`storage.py`、`audit.py`、`clock.py`、`errors.py`、`api.py`、`acceptance.py`
  - 收费清算域：`toll_models.py`（数据对象）、`toll_engine.py`（纯函数计费引擎）、`toll_service.py`（政策/行程/事件/计费/账簿服务）、`toll_acceptance.py`（离线端到端验收）
- `tests/`：基础规则与收费域的单元、接口、事务边界、端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite
- 金额一律以人民币“分”为单位的整数运算，禁止浮点，保证逐分可核对

## 领域规则与不变量

### 政策（不可变版本 + 生效区间 + 优先级）

- 类型：`rate` 费率、`discount` 折扣（千分比 `permille` 或定额 `reduction`）、`cap` 封顶（默认整程封顶，`level=segment` 为路段级）、`exemption` 豁免。
- 作用域 `scope`：`segment_ids` / `organization_ids` / `vehicle_classes` / `tags`（车辆声明标签与政策标签有交集即命中）/ `hours`（夜间小时窗口 `[起,止)`，支持跨夜）。
- 政策版本一经发布内容不可变；发新版本时上一版本在新生效时点自动失效；撤回把状态置为 `withdrawn`，只对尚未关账的行程重新计费。
- 优惠承担方为政策发布机构 `published_org`（如省级政策中心），收入仍归路段运营方。

### 优惠顺序（每段独立计算，整程封顶最后作用）

1. **豁免**：优先级最高，命中即该段免费；
2. **费率**：取优先级最高的适用费率，否则用路段备案费率；
3. **折扣**：按优先级从小到大依次链式作用（夜间货运、节假日等可叠加）；
4. **路段级封顶**：取最低封顶；
5. **整程封顶**：取最低封顶，减免额按各段折后金额比例分摊（最大余数法，一分不差）。
6. **临时免费事件**：登记后残余应收全部免除，承担方为事件指定机构。

### 行程、事件与计费事实

- 行程开启时锁定经过的**路段版本**（跨路段按顺序登记）；路段后续调价不影响已开行程。
- 事件（`entry` / `exit` / `free_pass` / `closure_detour`）只追加；出口补录使旧出口 `active=0` 而非删除。
- 缺失出口可转 `pending_evidence`；出口到达后生成 `final` 计费事实。
- 计费事实以“行程 + 锁定路段版本 + 实际生效政策版本 + 车辆资格 + 出口时间 + 免费/绕行事件”的内容摘要去重，**不可重复**；相同输入永远复用同一事实。
- 已 `settled` 的行程拒绝迟到出口/免费/绕行直接改写收入，只能走退款/追缴凭证。

### 守恒账簿与争议

- 结算：借“应收”= 贷“收入（运营方）”+ 贷“优惠承担（政策机构）”，每段恒有 `gross = revenue + discount_borne`。
- 退款：不得超过未冻结（或关联争议冻结）的收入，按各运营方收入比例分摊回主体。
- 追缴：新增应收，按结算收入占比分摊。每张凭证落库前强制借贷平衡校验。
- 争议：只冻结该行程的分录；驳回解冻，成立可在冻结额度内退款。

### 解释与历史重放

- `GET /toll/trips/{id}`：返回行程、事件、锁定路段版本、计费明细（含每步优惠的政策与承担机构）、结算/调整分录、争议与各运营方份额。
- `GET /toll/trips/{id}/replay?as_of=...`：重放该时点可见的计费事实版本、清分分录与当时冻结状态。
- `GET /toll/trips/{id}/replay-pricing?basis_time=...`：不落库，按任意历史时点有效的政策重算金额。

## 主要 HTTP 接口

写入接口均需 `X-Actor-Id` 头与 `request_id`（幂等），角色：政策发布需 `admin/reviewer`，运营操作需 `admin/operator`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/toll/segments` / `/toll/segment-versions` | 登记路段 / 发布路段新版本 |
| POST | `/toll/policies` / `/toll/policy-versions` / `/toll/policies/withdraw` | 发布政策 / 新版本 / 撤回 |
| GET | `/toll/policies[?active_at=]` / `/toll/policies/{id}` | 政策查询 |
| POST | `/toll/trips` | 开行程（跨路段、入口证据、车辆标签） |
| POST | `/toll/trips/pending-evidence` | 缺出口转待补证 |
| POST | `/toll/trips/exit` | 出口事件/补录（触发计费） |
| POST | `/toll/trips/free-pass` / `/toll/trips/detour` | 临时免费 / 封路绕行 |
| POST | `/toll/trips/settle` | 关账并生成分账分录 |
| POST | `/toll/refunds` / `/toll/surcharges` | 退款 / 追缴 |
| POST | `/toll/disputes` / `/toll/disputes/resolve` | 争议冻结 / 处理 |
| GET | `/toll/trips/{id}` / `/toll/trips/{id}/ledger` | 金额解释 / 分录 |
| GET | `/toll/trips/{id}/replay` / `.../replay-pricing` | 历史重放 |

基础域接口（`/organizations`、`/actors`、`/sites`、`/domain-records`、`/audit-events`、`/health`）保持不变。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

基础域：

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
```

收费清算域（差异化费率顺序、待补证、跨路段分账、退款守恒、争议冻结、撤回边界、历史重放）：

```bash
PYTHONPATH=src python3 -m transport_coordination.toll_acceptance
```

成功时各输出一行 `status` 为 `ok` 的 JSON，退出码 `0`。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态、计费事实、账簿分录与审计历史继续保留。
