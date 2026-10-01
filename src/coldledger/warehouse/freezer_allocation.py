from dataclasses import dataclass

@dataclass(frozen=True)
class Slot: slot_id:str; zone:str; circuit:str; neighbors:frozenset
@dataclass(frozen=True)
class Load: load_id:str; zone:str; high_value:bool; redundancy:bool

@dataclass(frozen=True)
class SplitResult:
    placement: dict
    unplaced: tuple


def _eligible(load, slot, result, circuits):
    # Temperature-zone qualification is physical: a slot in another zone can
    # never hold the load, regardless of free capacity.
    if slot.zone != load.zone:
        return False
    neighbor_loads = {result[n] for n in slot.neighbors if n in result}
    if load.high_value and load.load_id in neighbor_loads:
        return False
    if load.redundancy and slot.circuit in circuits.get(load.load_id, set()):
        return False
    return True


def allocate(loads, slots, occupied, on_shortage="reject"):
    """Place loads into slots subject to one combined constraint:

    slot temperature zone must qualify, and the slot must survive the
    high-value neighbor and redundant-circuit restrictions on top of free
    capacity and existing reservations (``occupied``).

    on_shortage="reject" (default) refuses the entire batch atomically: no
    input occupancy is mutated and ValueError is raised without leaving any
    load in an unqualified slot. on_shortage="split" migrates whatever fits
    and reports the remainder in a SplitResult.
    """
    if on_shortage not in {"reject", "split"}:
        raise ValueError("on_shortage must be 'reject' or 'split'")
    by = {s.slot_id: s for s in slots}
    # Work on a copy: on rejection the caller's occupancy stays untouched and
    # no load remains parked in an unqualified slot.
    result = dict(occupied)
    circuits = {}
    for sid, lid in occupied.items():
        circuits.setdefault(lid, set()).add(by[sid].circuit)
    for load in sorted(loads, key=lambda x: (not x.high_value, x.load_id)):
        if load.load_id in result.values():
            continue
        cand = [s for s in slots if s.slot_id not in result and _eligible(load, s, result, circuits)]
        if not cand:
            if on_shortage == "reject":
                raise ValueError(f"no qualified freezer slot for {load.load_id}")
            continue
        c = min(cand, key=lambda s: (sum(n in result for n in s.neighbors), s.slot_id))
        result[c.slot_id] = load.load_id
        circuits.setdefault(load.load_id, set()).add(c.circuit)
    if on_shortage == "reject":
        return result
    unplaced = tuple(
        load.load_id
        for load in sorted(loads, key=lambda x: (not x.high_value, x.load_id))
        if load.load_id not in result.values()
    )
    return SplitResult(result, unplaced)
