"""纯函数计费引擎：按历史时点选择政策版本并计算金额。

金额全部以人民币“分”为最小单位的整数进行运算，禁止浮点，
从机制上保证后续清分、退款、追缴可以逐分核对、金额守恒。
"""

from __future__ import annotations

from typing import Any

from .audit import canonical_json, digest

# 政策类型
RATE = "rate"            # 费率（分/公里）
DISCOUNT = "discount"    # 折扣（按优先级顺序作用）
CAP = "cap"              # 封顶（默认整程封顶，可选路段级）
EXEMPTION = "exemption"  # 豁免（命中即免费）
POLICY_KINDS = frozenset({RATE, DISCOUNT, CAP, EXEMPTION})


def scope_matches(scope: dict[str, Any], segment_id: str, organization_id: str,
                  vehicle: dict[str, Any], at: str) -> bool:
    """判断政策作用域是否覆盖给定路段、车辆与事件时点。

    scope 支持 segment_ids / organization_ids / vehicle_classes / tags / hours，
    tags 语义为“声明标签与政策标签存在交集”；hours 为 [起,止) 小时窗口，
    支持跨夜（如 [22, 6)）；空 scope 表示全域。
    """

    segment_ids = scope.get("segment_ids")
    if segment_ids is not None and segment_id not in segment_ids:
        return False
    organization_ids = scope.get("organization_ids")
    if organization_ids is not None and organization_id not in organization_ids:
        return False
    vehicle_classes = scope.get("vehicle_classes")
    if vehicle_classes is not None and vehicle["vehicle_class"] not in vehicle_classes:
        return False
    tags = scope.get("tags")
    if tags is not None and not (set(vehicle.get("tags", ())) & set(tags)):
        return False
    hours = scope.get("hours")
    if hours is not None:
        start, end = int(hours[0]), int(hours[1])
        hour = int(at[11:13])
        if start < end:
            if not start <= hour < end:
                return False
        elif not (hour >= start or hour < end):
            return False
    return True


def active_policy(policy: dict[str, Any], at: str) -> bool:
    """政策在指定历史时点是否有效（已发布、生效、未过期、未撤回）。"""

    return (
        policy["status"] == "active"
        and policy["effective_at"] <= at
        and (policy["expires_at"] is None or at < policy["expires_at"])
    )


def _round_half_up(value: int, denominator: int) -> int:
    """对 value/denominator 做四舍五入（value、denominator 均非负）。"""

    return (2 * value + denominator) // (2 * denominator) if denominator else value


def _proportional(weights: list[int], amount: int) -> list[int]:
    """按权重把整数金额分配到各项，最大余数法保证合计一分不差。"""

    total = sum(weights)
    if total == 0:
        return [0] * len(weights)
    if amount >= total:
        return list(weights)
    shares = [amount * w // total for w in weights]
    remainder = amount - sum(shares)
    order = sorted(range(len(weights)),
                   key=lambda i: (-(amount * weights[i] % total), -weights[i], i))
    for index in order:
        if remainder == 0:
            break
        if shares[index] < weights[index]:
            shares[index] += 1
            remainder -= 1
    return shares


def price_trip(*, segments: list[dict[str, Any]], policies: list[dict[str, Any]],
               vehicle: dict[str, Any], basis_time: str) -> dict[str, Any]:
    """依据路段、当时有效政策与车辆资格计算一次通行的金额。

    返回结构化结果：
    - per_segment：逐路段的原价、折扣链、封顶、最终价、优惠承担依据；
    - total_fen：向用户收取的总金额；
    - total_gross_fen：优惠前总金额；
    - total_discount_fen：优惠合计；
    - policy_versions：实际参与计算的政策版本（供解释与重放）。
    封顶默认作用于整程（params.level='segment' 时为路段级）。
    """

    used_versions: set[tuple[str, int]] = set()
    per_segment: list[dict[str, Any]] = []
    total_gross = 0

    for segment in segments:
        segment_id = segment["segment_id"]
        organization_id = segment["organization_id"]
        applicable = [p for p in policies
                      if active_policy(p, basis_time)
                      and scope_matches(p["scope"], segment_id, organization_id, vehicle, basis_time)]

        # 豁免优先级最高：命中即该路段免费
        exemptions = sorted((p for p in applicable if p["kind"] == EXEMPTION),
                            key=lambda p: (p["priority"], p["policy_id"]))
        if exemptions:
            chosen = exemptions[0]
            used_versions.add((chosen["policy_id"], chosen["version"]))
            gross = _round_half_up(segment["rate_fen_per_km"] * segment["length_m"], 1000)
            total_gross += gross
            per_segment.append({
                "segment_id": segment_id,
                "organization_id": organization_id,
                "length_m": segment["length_m"],
                "gross_fen": gross,
                "rate_source": None,
                "discounts": [],
                "segment_cap": None,
                "final_fen": 0,
                "discount_fen": gross,
                "exemption": {"policy_id": chosen["policy_id"], "version": chosen["version"],
                              "label": chosen["params"].get("label", chosen["policy_id"])},
            })
            continue

        # 费率：同一时点取优先级最高的一条，否则用路段备案费率
        rate_policies = [p for p in applicable if p["kind"] == RATE]
        if rate_policies:
            chosen = min(rate_policies, key=lambda p: (p["priority"], p["policy_id"]))
            rate_fen_per_km = int(chosen["params"]["rate_fen_per_km"])
            used_versions.add((chosen["policy_id"], chosen["version"]))
            rate_source = {"policy_id": chosen["policy_id"], "version": chosen["version"]}
        else:
            rate_fen_per_km = segment["rate_fen_per_km"]
            rate_source = None
        gross = _round_half_up(rate_fen_per_km * segment["length_m"], 1000)
        total_gross += gross

        # 折扣：按优先级从小到大依次作用
        current = gross
        discount_chain: list[dict[str, Any]] = []
        for policy in sorted((p for p in applicable if p["kind"] == DISCOUNT),
                             key=lambda p: (p["priority"], p["policy_id"])):
            before = current
            params = policy["params"]
            mode = params.get("mode", "percent")
            if mode == "percent":
                permille = int(params.get("permille", 1000))  # 千分之，1000=原价，900=9折
                current = _round_half_up(before * permille, 1000)
                reduced = before - current
            elif mode == "reduction":
                reduced = min(before, int(params["amount_fen"]))
                current = before - reduced
            else:
                raise ValueError(f"未知折扣模式: {mode}")
            used_versions.add((policy["policy_id"], policy["version"]))
            discount_chain.append({
                "policy_id": policy["policy_id"], "version": policy["version"],
                "label": params.get("label", policy["policy_id"]),
                "mode": mode, "before_fen": before, "after_fen": current,
                "discount_fen": reduced,
            })

        # 路段级封顶（params.level == 'segment'）：取最低封顶
        segment_cap = None
        final_fen = current
        seg_caps = [p for p in applicable
                    if p["kind"] == CAP and p["params"].get("level", "trip") == "segment"]
        for policy in sorted(seg_caps, key=lambda p: (int(p["params"]["max_fen"]),
                                                      p["priority"], p["policy_id"])):
            used_versions.add((policy["policy_id"], policy["version"]))
            max_fen = int(policy["params"]["max_fen"])
            if final_fen > max_fen:
                final_fen = max_fen
                segment_cap = {"policy_id": policy["policy_id"], "version": policy["version"],
                               "label": policy["params"].get("label", policy["policy_id"]),
                               "max_fen": max_fen}

        per_segment.append({
            "segment_id": segment_id,
            "organization_id": organization_id,
            "length_m": segment["length_m"],
            "gross_fen": gross,
            "rate_source": rate_source,
            "discounts": discount_chain,
            "segment_cap": segment_cap,
            "final_fen": final_fen,
            "discount_fen": gross - final_fen,
            "exemption": None,
        })

    # 整程封顶：取最低封顶，按各路段折后金额比例分摊减免
    trip_caps = [p for p in policies
                 if active_policy(p, basis_time) and p["kind"] == CAP
                 and p["params"].get("level", "trip") == "trip"]
    trip_cap_applied = None
    subtotal = sum(item["final_fen"] for item in per_segment)
    if trip_caps and per_segment:
        chosen = min(trip_caps, key=lambda p: (int(p["params"]["max_fen"]),
                                               p["priority"], p["policy_id"]))
        # 整程封顶的作用域至少需覆盖行程所有收费路段
        covers_all = all(
            scope_matches(chosen["scope"], item["segment_id"], item["organization_id"],
                          vehicle, basis_time)
            for item in per_segment)
        if covers_all:
            used_versions.add((chosen["policy_id"], chosen["version"]))
            max_fen = int(chosen["params"]["max_fen"])
            if subtotal > max_fen:
                relief = subtotal - max_fen
                weights = [item["final_fen"] for item in per_segment]
                parts = _proportional(weights, relief)
                for item, part in zip(per_segment, parts):
                    item["trip_cap_relief_fen"] = part
                    item["trip_cap_policy_id"] = chosen["policy_id"]
                    item["final_fen"] -= part
                    item["discount_fen"] = item["gross_fen"] - item["final_fen"]
                trip_cap_applied = {"policy_id": chosen["policy_id"], "version": chosen["version"],
                                    "label": chosen["params"].get("label", chosen["policy_id"]),
                                    "max_fen": max_fen, "relief_fen": relief}

    total = sum(item["final_fen"] for item in per_segment)
    policy_versions = [{"policy_id": pid, "version": ver} for pid, ver in sorted(used_versions)]
    return {
        "basis_time": basis_time,
        "per_segment": per_segment,
        "trip_cap_applied": trip_cap_applied,
        "total_fen": total,
        "total_gross_fen": total_gross,
        "total_discount_fen": total_gross - total,
        "policy_versions": policy_versions,
    }


def build_input_hash(*, trip_id: str, segments: list[dict[str, Any]],
                     applied_policies: list[dict[str, Any]],
                     vehicle: dict[str, Any], basis_time: str,
                     extra_event_ids: list[str] | None = None) -> str:
    """对实际参与计费的输入版本求摘要，保证计费事实不可重复。

    计费只取决于行程身份、锁定的路段版本、真正生效的政策版本、车辆资格、
    出口时间（basis_time）以及临时免费/封路绕行事件；
    出口事件行本身被同时间的补录事件取代时复用同一事实。
    """

    material = {
        "trip_id": trip_id,
        "segments": [{"segment_id": s["segment_id"], "version": s["version"],
                      "rate_fen_per_km": s["rate_fen_per_km"], "length_m": s["length_m"]}
                     for s in sorted(segments, key=lambda s: s["segment_id"])],
        "applied_policies": sorted(
            ({"policy_id": p["policy_id"], "version": p["version"]} for p in applied_policies),
            key=lambda p: (p["policy_id"], p["version"])),
        "vehicle": vehicle,
        "basis_time": basis_time,
        "extra_event_ids": sorted(extra_event_ids or []),
    }
    return digest(material)
