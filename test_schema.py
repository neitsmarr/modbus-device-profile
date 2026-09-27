#!/usr/bin/env python3
"""Negative tests for device-profile.schema.json.

A schema that accepts everything passes the happy path too, so the only way to
know it is doing work is to feed it mutations of a known-good profile and check
each one is rejected. Every 'reject' case is a mistake someone will plausibly
make while hand-editing a profile -- which, per DECISIONS.md D5, is how every
profile gets written.

Usage:  python test_schema.py
"""

from __future__ import annotations

import copy
import io
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))
from validate import normalize


def main() -> int:
    try:
        import jsonschema
        import yaml
    except ImportError:
        sys.exit("test_schema.py needs: python -m pip install pyyaml jsonschema")

    schema = json.loads((HERE / "device-profile.schema.json").read_text(encoding="utf-8"))
    with io.open(HERE / "device-profile.yaml", encoding="utf-8") as fh:
        base = yaml.safe_load(fh)
    validator = jsonschema.Draft202012Validator(schema)

    failures: list[str] = []

    baseline = list(validator.iter_errors(normalize(base)))
    if baseline:
        failures.append(f"baseline profile does not satisfy its own schema ({len(baseline)} errors)")
        for err in baseline[:6]:
            print("   baseline:", "/".join(str(p) for p in err.path), err.message[:120])

    def check(expect: str, name: str, mutate) -> None:
        doc = copy.deepcopy(base)
        mutate(doc)
        n = len(list(validator.iter_errors(normalize(doc))))
        ok = (n > 0) if expect == "reject" else (n == 0)
        print(f"   {'pass' if ok else 'FAIL'}  {expect}: {name}")
        if not ok:
            failures.append(f"{expect}: {name}")

    reject = lambda name, m: check("reject", name, m)
    accept = lambda name, m: check("accept", name, m)

    sig = lambda d, n: d["signals"][n]
    proc = lambda d, n: d["procedures"][n]

    print("-- device section")
    reject("missing device section", lambda d: d.pop("device"))
    reject("device without firmware_version", lambda d: d["device"].pop("firmware_version"))
    reject("firmware_version not major.minor", lambda d: d["device"].update({"firmware_version": "v1"}))
    reject("device type as string", lambda d: d["device"].update({"type": "4010"}))

    print("-- vocabulary")
    reject("misspelled key (writeable_when)", lambda d: sig(d, "ventilation_level").update({"writeable_when": "x"}))
    reject("unknown top-level section", lambda d: d.update({"signal": {}}))
    reject("the removed registers section", lambda d: d.update({"registers": {"unlock": {"binding": {"holding": 1}}}}))
    reject("CamelCase signal name", lambda d: d["signals"].update({"SupplyFanSpeed": sig(d, "supply_fan_speed")}))
    reject("unknown data type", lambda d: sig(d, "supply_fan_speed")["binding"].update({"type": "uint12"}))
    reject("invented access level", lambda d: sig(d, "ventilation_level").update({"access": "commissioning"}))

    print("-- bindings")
    reject("signal with no binding", lambda d: sig(d, "ventilation_level").pop("binding"))
    reject("two tables in one binding", lambda d: sig(d, "supply_fan_speed")["binding"].update({"holding": 5}))
    reject("address above 65535", lambda d: sig(d, "supply_fan_speed")["binding"].update({"input": 70000}))
    reject("bit without type bool", lambda d: sig(d, "supply_fan_speed")["binding"].update({"bit": 3}))
    reject("function code not in Modbus", lambda d: d["access"]["supported_fc"].append(99))
    reject("max_read_words above the Modbus limit", lambda d: d["access"].update({"max_read_words": 200}))

    print("-- enum and flags")
    reject("both enum and flags on one signal",
           lambda d: sig(d, "supply_fan_state").update({"enum": {0: "ok"}}))
    reject("flag bit index above 15",
           lambda d: sig(d, "supply_fan_state")["flags"].update({16: "impossible_bit"}))
    reject("enum member with a space",
           lambda d: sig(d, "operating_mode")["enum"].update({9: "not valid"}))
    reject("empty flags map", lambda d: sig(d, "supply_fan_state").update({"flags": {}}))

    print("-- presentation")
    reject("range with three numbers", lambda d: sig(d, "ventilation_level").update({"range": [1, 2, 3]}))
    reject("scale of zero", lambda d: sig(d, "mcu_temperature").update({"scale": 0}))
    reject("negative decimals", lambda d: sig(d, "mcu_temperature").update({"decimals": -1}))
    reject("constraint with when and nothing else",
           lambda d: sig(d, "ventilation_level").update({"constraints": [{"when": "operating_mode == manual"}]}))

    print("-- procedures")
    reject("confirm without danger", lambda d: proc(d, "swap_air_chains").pop("danger"))
    reject("procedure with no steps", lambda d: proc(d, "swap_air_chains").pop("steps"))
    reject("await without timeout",
           lambda d: proc(d, "rescan_sensor_channels")["steps"][1]["await"].pop("timeout_ms"))
    reject("step with two verbs", lambda d: proc(d, "swap_air_chains")["steps"][2].update({"delay_ms": 5}))
    reject("write step without a value", lambda d: proc(d, "swap_air_chains")["steps"][0]["write"].pop("value"))
    reject("write step targeting a raw binding",
           lambda d: proc(d, "swap_air_chains")["steps"][0]["write"].update({"binding": "unlock"}))
    reject("duplicate entries in reread",
           lambda d: proc(d, "rescan_sensor_channels")["steps"][2].update(
               {"reread": ["supply_ch1_sensor_capability", "supply_ch1_sensor_capability"]}))
    reject("condition with both equals and contains",
           lambda d: proc(d, "rescan_sensor_channels")["outcome"]["success"].update({"contains": "complete"}))

    print("-- must stay legal")
    accept("uppercase enum member", lambda d: sig(d, "operating_mode")["enum"].update({9: "ECO"}))
    accept("bit flag as bool",
           lambda d: sig(d, "supply_digital_input_state")["binding"].update({"bit": 3, "type": "bool"}))
    accept("hidden service register", lambda d: sig(d, "internal_voltage_3v3").update({"hidden": True}))
    accept("contains in a precondition-bearing outcome",
           lambda d: proc(d, "swap_air_chains")["outcome"].update(
               {"failure": {"signal": "device_status_errors", "contains": "memory_fault"}}))
    def use_variants(d):
        s = sig(d, "supply_ch1_voc_level")
        for k in ("unit", "scale", "range", "decimals"):
            s.pop(k, None)
        s["variants"] = [
            {"effective_when": "operating_mode == manual", "unit": "index"},
            {"effective_when": "operating_mode == automatic", "unit": "ppb"},
        ]

    accept("variants instead of a fixed unit", use_variants)

    print()
    if failures:
        print(f"{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all schema tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
