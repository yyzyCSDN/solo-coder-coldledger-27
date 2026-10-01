import unittest
from decimal import Decimal
from coldledger.warehouse.cold_capacity import (
    Allocation, FreezerPool, PalletNeed, plan, rebalance, summarize, validate_plan,
)
from coldledger.warehouse.freezer_allocation import Load, Slot, allocate

D = Decimal


class ColdCapacityTests(unittest.TestCase):
    def test_zone_mismatch_rejected_even_with_spare_capacity(self):
        needs = [PalletNeed("p1", D("5"), 1, "ULT")]
        pools = [FreezerPool("f-wrong", D("100"), "FROZEN")]
        with self.assertRaises(ValueError):
            plan(needs, pools)

    def test_capacity_and_reserve_checked_together(self):
        needs = [PalletNeed("p1", D("8"), 1, "ULT")]
        with self.assertRaises(ValueError):
            plan(needs, [FreezerPool("f1", D("10"), "ULT", reserve=D("4"))])
        rows = plan(needs, [FreezerPool("f1", D("10"), "ULT", reserve=D("2"))])
        self.assertEqual(summarize(rows), {"f1": D("8")})

    def test_split_within_zone_never_cross_zone(self):
        needs = [PalletNeed("p1", D("6"), 2, "ULT"), PalletNeed("p2", D("6"), 1, "ULT")]
        pools = [
            FreezerPool("f1", D("8"), "ULT"),
            FreezerPool("f2", D("8"), "ULT"),
            FreezerPool("f3", D("100"), "FROZEN"),
        ]
        rows = plan(needs, pools)
        validate_plan(rows, needs, pools)
        self.assertEqual(set(summarize(rows)), {"f1", "f2"})

    def test_whole_batch_rejected_when_in_zone_capacity_short(self):
        needs = [PalletNeed("p1", D("6"), 2, "ULT"), PalletNeed("p2", D("6"), 1, "ULT")]
        tight = [
            FreezerPool("f1", D("8"), "ULT"),
            FreezerPool("f2", D("2"), "ULT"),
            FreezerPool("f3", D("100"), "FROZEN"),
        ]
        with self.assertRaises(ValueError):
            plan(needs, tight)

    def test_validate_plan_catches_zone_mismatch(self):
        needs = [PalletNeed("p1", D("5"), 1, "ULT")]
        pools = [FreezerPool("f1", D("10"), "FROZEN")]
        with self.assertRaises(ValueError):
            validate_plan([Allocation("p1", "f1", D("5"), D("0"))], needs, pools)

    def test_rebalance_migrates_within_zone(self):
        needs = [PalletNeed("p1", D("4"), 1, "ULT"), PalletNeed("p2", D("4"), 1, "ULT")]
        pools = [
            FreezerPool("f1", D("4"), "ULT"),
            FreezerPool("f2", D("4"), "ULT"),
            FreezerPool("f3", D("8"), "ULT"),
        ]
        rows = plan(needs, pools)
        moved = rebalance(rows, "f1", needs, pools)
        validate_plan(moved, needs, pools)
        self.assertNotIn("f1", summarize(moved))

    def test_rebalance_rejects_when_only_cross_zone_remains(self):
        needs = [PalletNeed("p1", D("4"), 1, "ULT")]
        pools = [FreezerPool("f1", D("4"), "ULT"), FreezerPool("f3", D("50"), "FROZEN")]
        rows = plan(needs, pools)
        with self.assertRaises(ValueError):
            rebalance(rows, "f1", needs, pools)


class FreezerAllocationTests(unittest.TestCase):
    def test_load_placed_only_in_matching_zone(self):
        slots = [
            Slot("s1", "ULT", "c1", frozenset()),
            Slot("s2", "FROZEN", "c1", frozenset()),
        ]
        out = allocate([Load("l1", "FROZEN", False, False)], slots, {})
        self.assertEqual(out, {"s2": "l1"})

    def test_failure_leaves_occupied_untouched(self):
        slots = [
            Slot("s1", "ULT", "c1", frozenset()),
            Slot("s9", "ULT", "c2", frozenset()),
        ]
        occupied = {"s9": "other"}
        with self.assertRaises(ValueError):
            allocate([Load("l1", "FROZEN", False, False)], slots, occupied)
        self.assertEqual(occupied, {"s9": "other"})


if __name__ == "__main__":
    unittest.main()
