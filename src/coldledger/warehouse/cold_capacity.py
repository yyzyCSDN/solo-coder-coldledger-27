from __future__ import annotations
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Iterable

@dataclass(frozen=True)
class PalletNeed:
    key: str
    demand: Decimal
    priority: int
    group: str
    risk: float = 0.0
    locked: bool = False
    zone: str = ""

    def validate(self) -> None:
        if self.demand <= 0:
            raise ValueError("demand must be positive")
        if not 0.0 <= self.risk <= 1.0:
            raise ValueError("risk outside 0..1")
        if not self.group:
            raise ValueError("group required")
        if not self.zone:
            raise ValueError("zone required")

@dataclass(frozen=True)
class FreezerPool:
    key: str
    capacity: Decimal
    group: str
    enabled: bool = True
    reserve: Decimal = Decimal("0")
    zone: str = ""

    @property
    def usable(self) -> Decimal:
        value = self.capacity - self.reserve
        return max(Decimal("0"), value)

@dataclass(frozen=True)
class Allocation:
    demand_key: str
    resource_key: str
    quantity: Decimal
    score: Decimal


def _eligible(d: PalletNeed, r: FreezerPool, allow_cross_group: bool) -> bool:
    # Temperature-zone qualification is a physical constraint and can never be
    # bypassed, not even when cross-group allocation is requested.
    if r.zone != d.zone:
        return False
    if not allow_cross_group and r.group != d.group:
        return False
    return True


def _score(d: PalletNeed, r: FreezerPool, remaining: Decimal) -> Decimal:
    group_penalty = Decimal("0") if d.group == r.group else Decimal("7.5")
    risk_penalty = Decimal(str(d.risk)) * Decimal("10")
    slack = max(Decimal("0"), remaining - d.demand)
    priority_credit = Decimal(d.priority) * Decimal("0.25")
    return slack + group_penalty + risk_penalty - priority_credit


def plan(
    demands: Iterable[PalletNeed],
    resources: Iterable[FreezerPool],
    allow_cross_group: bool = False,
    on_shortage: str = "reject",
) -> list[Allocation]:
    ds = list(demands)
    rs = list(resources)
    if on_shortage not in {"reject", "split"}:
        raise ValueError("on_shortage must be 'reject' or 'split'")
    if not rs and ds:
        raise ValueError("no resources")
    remaining = {r.key: r.usable for r in rs if r.enabled}
    by_key = {r.key: r for r in rs}
    result: list[Allocation] = []

    def allocate(d: PalletNeed) -> Decimal:
        # Zone qualification, remaining cold capacity and pool reserve are
        # evaluated together against a single working snapshot.
        candidates = []
        for rk, left in remaining.items():
            r = by_key[rk]
            if left <= 0:
                continue
            if not _eligible(d, r, allow_cross_group):
                continue
            candidates.append((_score(d, r, left), rk))
        if not candidates:
            raise ValueError(f"unallocated pallet: {d.key}")
        outstanding = d.demand
        for _, rk in sorted(candidates, key=lambda x: (x[0], x[1])):
            if outstanding <= 0:
                break
            q = min(outstanding, remaining[rk])
            if q <= 0:
                continue
            result.append(Allocation(d.key, rk, q, _score(d, by_key[rk], remaining[rk])))
            remaining[rk] -= q
            outstanding -= q
        return outstanding

    ordered = sorted(ds, key=lambda x: (-x.priority, -x.risk, x.key))
    if on_shortage == "reject":
        # Stage the whole batch against a scratch snapshot first; nothing is
        # committed until every demand fits. On failure the scratch state is
        # discarded, so no erroneous reservation survives.
        committed = list(result)
        snapshot = dict(remaining)
        try:
            for d in ordered:
                d.validate()
                if d.locked:
                    continue
                if allocate(d) > 0:
                    raise ValueError(f"insufficient freezer capacity for {d.key}")
        except ValueError:
            result.clear()
            result.extend(committed)
            remaining.clear()
            remaining.update(snapshot)
            raise
        return list(result)
    for d in ordered:
        d.validate()
        if d.locked:
            continue
        allocate(d)
    return result


def summarize(rows: Iterable[Allocation]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for row in rows:
        totals[row.resource_key] = totals.get(row.resource_key, Decimal("0")) + row.quantity
    return totals


def validate_plan(
    rows: Iterable[Allocation],
    demands: Iterable[PalletNeed],
    resources: Iterable[FreezerPool],
    allow_partial: bool = False,
) -> None:
    allocations = list(rows)
    demand_map = {d.key: d for d in demands}
    resource_map = {r.key: r for r in resources}
    by_demand: dict[str, Decimal] = {}
    by_resource: dict[str, Decimal] = {}
    for row in allocations:
        if row.demand_key not in demand_map or row.resource_key not in resource_map:
            raise ValueError("plan references unknown key")
        if row.quantity <= 0:
            raise ValueError("allocation must be positive")
        need = demand_map[row.demand_key]
        pool = resource_map[row.resource_key]
        if pool.zone != need.zone:
            raise ValueError(f"temperature zone mismatch for {need.key}")
        by_demand[row.demand_key] = by_demand.get(row.demand_key, Decimal("0")) + row.quantity
        by_resource[row.resource_key] = by_resource.get(row.resource_key, Decimal("0")) + row.quantity
    for key, d in demand_map.items():
        if d.locked:
            continue
        placed = by_demand.get(key, Decimal("0"))
        if allow_partial:
            if placed > d.demand:
                raise ValueError("demand over-allocated")
        elif placed != d.demand:
            raise ValueError("demand not exactly satisfied")
    for key, used in by_resource.items():
        if used > resource_map[key].usable:
            raise ValueError("resource over capacity")


def rebalance(
    rows: list[Allocation],
    disabled_resource: str,
    demands: list[PalletNeed],
    resources: list[FreezerPool],
    on_shortage: str = "reject",
) -> list[Allocation]:
    if disabled_resource not in {r.key for r in resources}:
        return rows
    impacted = {r.demand_key for r in rows if r.resource_key == disabled_resource}
    keep = [r for r in rows if r.demand_key not in impacted]
    reserved = summarize(keep)
    adjusted_resources = []
    for r in resources:
        enabled = r.enabled and r.key != disabled_resource
        extra_reserve = reserved.get(r.key, Decimal("0"))
        adjusted_resources.append(replace(r, enabled=enabled, reserve=r.reserve + extra_reserve))
    retry = [d for d in demands if d.key in impacted]
    try:
        return keep + plan(retry, adjusted_resources, allow_cross_group=True, on_shortage=on_shortage)
    except ValueError:
        if on_shortage == "reject":
            # The migration failed; the original placement stays authoritative.
            return rows
        raise


def explain(rows: Iterable[Allocation]) -> list[str]:
    return [f"{row.demand_key} -> {row.resource_key}: {row.quantity}" for row in rows]

def utilization(rows, resources):
    used=summarize(rows); return {r.key: float(used.get(r.key,0)/r.usable) if r.usable else 0.0 for r in resources}
