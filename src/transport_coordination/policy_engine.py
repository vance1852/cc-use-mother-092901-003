"""收费政策的纯函数求值引擎。

引擎不访问数据库，输入为不可变的政策版本集合、行程路段和车辆上下文，
输出按优先级逐步求值的计费轨迹，用于解释“优惠顺序”和按历史时点重放。

金额单位统一为整数分（fen），避免浮点误差。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

POLICY_KINDS = ("rate", "cap", "exemption", "eligibility")
NIGHT_START = 22
NIGHT_END = 6


@dataclass(frozen=True)
class Policy:
    """表示一个不可变的政策版本。"""

    policy_id: str
    version: int
    kind: str
    priority: int
    name: str
    conditions: dict[str, Any]
    effect: dict[str, Any]
    effective_from: datetime
    effective_to: datetime | None
    published_at: datetime
    withdrawn_at: datetime | None = None

    def in_force(self, at: datetime) -> bool:
        """判断政策在给定历史时点是否有效（含已发布、生效区间、未撤回）。"""

        if self.published_at > at or self.effective_from > at:
            return False
        if self.effective_to is not None and at >= self.effective_to:
            return False
        if self.withdrawn_at is not None and self.withdrawn_at <= at:
            return False
        return True


@dataclass(frozen=True)
class ChargeLine:
    """表示计费路径上的一条路段行。"""

    segment_id: str
    operator_id: str
    base_amount_fen: int


@dataclass
class _Line:
    segment_id: str
    operator_id: str
    base_fen: int
    amount_fen: int


def is_night_hour(moment: datetime) -> bool:
    """22:00-次日06:00 视为夜间（按时间戳自带的本地墙钟小时）。"""

    return moment.hour >= NIGHT_START or moment.hour < NIGHT_END


def _global_match(conditions: dict[str, Any], context: dict[str, Any]) -> bool:
    """评估与路段无关的全局条件。"""

    if "vehicle_classes" in conditions and context["vehicle_class"] not in conditions["vehicle_classes"]:
        return False
    if "tags" in conditions and not (set(conditions["tags"]) & context["tags"]):
        return False
    if "is_holiday" in conditions and bool(conditions["is_holiday"]) is not bool(context["is_holiday"]):
        return False
    if conditions.get("night") and not context["night"]:
        return False
    return True


def _targets(conditions: dict[str, Any], lines: list[_Line]) -> list[_Line]:
    """按 segment_ids 条件筛选政策作用的路段行；缺省作用于全部行。"""

    wanted = conditions.get("segment_ids")
    if not wanted:
        return list(lines)
    wanted_set = set(wanted)
    return [line for line in lines if line.segment_id in wanted_set]


def _discounted(amount: int, percent: int) -> int:
    """整数百分折扣，四舍五入到分。"""

    return (amount * (100 - percent) + 50) // 100


def _allocate_reduction(lines: list[_Line], reduction: int) -> None:
    """把封顶减免额按各行金额成比例分摊（最大余数法保证总额守恒）。"""

    total = sum(line.amount_fen for line in lines)
    if total <= 0 or reduction <= 0:
        return
    reduction = min(reduction, total)
    shares: list[tuple[int, int, _Line]] = []
    allocated = 0
    for index, line in enumerate(lines):
        exact = reduction * line.amount_fen
        floored = exact // total
        shares.append((exact - floored * total, -index, line))
        allocated += floored
        line.amount_fen -= floored
    remainder = reduction - allocated
    for _, _, line in sorted(shares, key=lambda item: (-item[0], item[1])):
        if remainder <= 0:
            break
        if line.amount_fen > 0:
            line.amount_fen -= 1
            remainder -= 1


def evaluate_charge(lines: list[ChargeLine], vehicle: dict[str, Any], is_holiday: bool,
                    charge_time: datetime, policies: list[Policy]) -> dict[str, Any]:
    """按政策优先级求值一次通行的完整计费轨迹。

    求值顺序固定为：资格规则先按优先级累积资格标签，随后费率、豁免、封顶
    按 (priority, policy_id) 依次作用；每条规则无论是否命中都进入轨迹。
    """

    working = [_Line(line.segment_id, line.operator_id, line.base_amount_fen, line.base_amount_fen)
               for line in lines]
    tags = set(vehicle.get("tags", ()))
    context = {
        "vehicle_id": vehicle.get("vehicle_id"),
        "vehicle_class": vehicle.get("vehicle_class"),
        "tags": tags,
        "is_holiday": bool(is_holiday),
        "night": is_night_hour(charge_time),
    }
    ordered = sorted(policies, key=lambda policy: (policy.priority, policy.policy_id, policy.version))
    steps: list[dict[str, Any]] = []
    order = 0

    # 第一阶段：资格规则按优先级授予资格标签。
    for policy in ordered:
        if policy.kind != "eligibility":
            continue
        order += 1
        matched = _global_match(policy.conditions, context)
        granted = list(policy.effect.get("grant_tags", ())) if matched else []
        if matched:
            tags.update(granted)
        steps.append({
            "order": order,
            "policy_id": policy.policy_id,
            "version": policy.version,
            "name": policy.name,
            "kind": policy.kind,
            "matched": matched,
            "applied": bool(granted),
            "detail": {"grant_tags": granted} if granted else {},
        })

    # 第二阶段：费率、豁免、封顶按优先级依次作用。
    for policy in ordered:
        if policy.kind not in ("rate", "exemption", "cap"):
            continue
        order += 1
        before = {line.segment_id: line.amount_fen for line in working}
        matched = _global_match(policy.conditions, context)
        targets = _targets(policy.conditions, working) if matched else []
        applied = False
        detail: dict[str, Any] = {}
        if matched and targets and sum(line.amount_fen for line in targets) > 0:
            if policy.kind == "exemption" and policy.effect.get("type") == "full":
                for line in targets:
                    line.amount_fen = 0
                applied = True
                detail = {"type": "full", "segments": [line.segment_id for line in targets]}
            elif policy.kind == "rate" and policy.effect.get("type") == "discount_pct":
                percent = int(policy.effect["percent"])
                for line in targets:
                    line.amount_fen = _discounted(line.amount_fen, percent)
                applied = True
                detail = {"type": "discount_pct", "percent": percent,
                          "segments": [line.segment_id for line in targets]}
            elif policy.kind == "cap" and policy.effect.get("type") == "amount_cap":
                cap = int(policy.effect["amount_fen"])
                subtotal = sum(line.amount_fen for line in targets)
                if subtotal > cap:
                    _allocate_reduction(targets, subtotal - cap)
                    applied = True
                    detail = {"type": "amount_cap", "cap_fen": cap,
                              "scope": "segments" if policy.conditions.get("segment_ids") else "trip",
                              "segments": [line.segment_id for line in targets]}
        after = {line.segment_id: line.amount_fen for line in working}
        steps.append({
            "order": order,
            "policy_id": policy.policy_id,
            "version": policy.version,
            "name": policy.name,
            "kind": policy.kind,
            "matched": matched,
            "applied": applied,
            "detail": detail,
            "before_total_fen": sum(before.values()),
            "after_total_fen": sum(after.values()),
        })

    segment_results = [
        {"segment_id": line.segment_id, "operator_id": line.operator_id,
         "base_fen": line.base_fen, "final_fen": line.amount_fen}
        for line in working
    ]
    return {
        "charge_time": charge_time.isoformat(),
        "context": {"vehicle_class": context["vehicle_class"], "tags": sorted(tags),
                    "is_holiday": context["is_holiday"], "night": context["night"]},
        "steps": steps,
        "segments": segment_results,
        "base_total_fen": sum(line.base_fen for line in working),
        "final_total_fen": sum(line.amount_fen for line in working),
    }


def policy_snapshot(policies: list[Policy]) -> list[dict[str, Any]]:
    """固化参与求值的政策版本，便于审计与重放。"""

    return [
        {
            "policy_id": policy.policy_id,
            "version": policy.version,
            "kind": policy.kind,
            "priority": policy.priority,
            "name": policy.name,
            "conditions": policy.conditions,
            "effect": policy.effect,
            "effective_from": policy.effective_from.isoformat(),
            "effective_to": policy.effective_to.isoformat() if policy.effective_to else None,
            "published_at": policy.published_at.isoformat(),
            "withdrawn_at": policy.withdrawn_at.isoformat() if policy.withdrawn_at else None,
        }
        for policy in sorted(policies, key=lambda policy: (policy.priority, policy.policy_id, policy.version))
    ]
