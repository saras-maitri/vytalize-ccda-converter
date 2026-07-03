#!/usr/bin/env python3
"""Regression tests for QA-identified converter defects.

Covers the two shipped fixes: dropped RX trailing-note medications, and
vaccine lot numbers containing lowercase. (A third defect — "NKDA" emitted as
a coded allergen — is intentionally not fixed here; the correct fix is a
negationInd="true" no-known-allergies observation, tracked separately.)

Run:  python test_fixes.py     (exit 0 = all pass)
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import converter as C


def enc(fields, date=datetime(2024, 1, 1)):
    e = C.Encounter("1", "VISIT", date)
    e.fields = list(fields)
    return e


def test_rx_with_trailing_note_not_dropped():
    """An RX line with a trailing '- note' after the sig must still be captured."""
    line = "Nexium 24HR 20 mg capsule,delayed release ( 1 by mouth daily ) - Pt buys OTC"
    e = enc([("RX", line)])
    meds = C.extract_medications([e])
    names = " ".join(m["name"] for m in meds)
    assert "Nexium 24HR" in names, f"OTC med dropped; got: {meds}"


def test_vaccine_lot_with_lowercase_captured():
    """A lot number containing lowercase (e.g. AC52b046BA) must be captured,
    and the route line ('Intramuscular') must NOT be mistaken for the lot."""
    block = "Tdap\n09/28/09\nIntramuscular\nAC52b046BA\nGlaxoSmithKline"
    e = enc([("FROZENSECTIONHTML_VaccineList", block)])
    vaccs = C.extract_immunizations([e])
    assert vaccs, "no vaccine extracted"
    lot = vaccs[0]["lot"]
    assert lot == "AC52b046BA", f"expected lot AC52b046BA, got {lot!r}"


def main():
    tests = [
        test_rx_with_trailing_note_not_dropped,
        test_vaccine_lot_with_lowercase_captured,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as ex:
            failed += 1
            print(f"FAIL  {t.__name__}: {ex}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
