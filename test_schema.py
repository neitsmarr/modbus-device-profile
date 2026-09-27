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
    reject("property with no kind", lambda d: sig(d, "ventilation_level").pop("kind"))
    reject("command that is not write_only",
           lambda d: sig(d, "device_reset").update({"access": "read_write"}))
    reject("measurement with a factory default",
           lambda d: sig(d, "supply_fan_speed").update({"default": 0}))
    reject("measurement that is writable",
           lambda d: sig(d, "supply_fan_speed").update({"access": "read_write"}))
    reject("binding with no address space", lambda d: sig(d, "supply_fan_speed")["binding"].pop("space"))
    reject("binding with no address", lambda d: sig(d, "supply_fan_speed")["binding"].pop("address"))
    reject("binding with no encoding", lambda d: sig(d, "supply_fan_speed")["binding"].pop("encoding"))
    reject("unknown address space", lambda d: sig(d, "supply_fan_speed")["binding"].update({"space": "register"}))
    reject("address above 65535", lambda d: sig(d, "supply_fan_speed")["binding"].update({"address": 70000}))
    reject("a coil carrying a whole integer",
           lambda d: sig(d, "ventilation_level")["binding"].update({"space": "coil"}))
    reject("a discrete input given a bit index",
           lambda d: sig(d, "error_memory_fault")["binding"].update({"space": "discrete", "bit": 2}))
    reject("a non-bool carried as a single bit",
           lambda d: sig(d, "supply_fan_speed")["binding"].update({"encoding": "bit", "bit": 3}))
    reject("a register bit with no bit index",
           lambda d: sig(d, "error_memory_fault")["binding"].pop("bit"))
    reject("function code not in Modbus", lambda d: d["access"]["supported_fc"].append(99))
    reject("max_read_words above the Modbus limit", lambda d: d["access"].update({"max_read_words": 200}))

    print("-- semantic type")
    reject("property with no semantic type", lambda d: sig(d, "ventilation_level").pop("type"))
    reject("type enum without an enum map",
           lambda d: sig(d, "operating_mode").pop("enum"))
    reject("enum map on a non-enum type",
           lambda d: sig(d, "ventilation_level").update({"enum": {0: "off"}}))
    reject("labels on something that is not a bool",
           lambda d: sig(d, "operating_mode").update({"labels": {"false": "a", "true": "b"}}))
    reject("bool labels missing the true state",
           lambda d: sig(d, "client_enable").update({"labels": {"false": "disabled"}}))
    reject("bitfield on a property that is not a bit",
           lambda d: sig(d, "ventilation_level").update({"bitfield": "Some Register"}))
    reject("bitfield on a property in coil space",
           lambda d: sig(d, "error_memory_fault")["binding"].update({"space": "coil"}))
    reject("scale on an enum",
           lambda d: sig(d, "operating_mode").update({"scale": 0.1}))
    reject("decimals on a bool",
           lambda d: sig(d, "client_enable").update({"decimals": 1}))
    reject("max_length on a non-text property",
           lambda d: sig(d, "ventilation_level").update({"max_length": 8}))
    reject("text without max_length",
           lambda d: sig(d, "ventilation_level").update({"type": "text"}))
    reject("unknown wire encoding",
           lambda d: sig(d, "ventilation_level")["binding"].update({"encoding": "uint12"}))
    reject("storage width that is not a real width",
           lambda d: sig(d, "ventilation_level").update({"storage": "int24"}))

    print("-- enum and labels")
    reject("enum member with a space",
           lambda d: sig(d, "operating_mode")["enum"].update({9: "not valid"}))
    reject("label with a space",
           lambda d: sig(d, "client_enable")["labels"].update({True: "not valid"}))
    reject("bit position above 15",
           lambda d: sig(d, "error_memory_fault")["binding"].update({"bit": 20}))

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
    reject("condition with both equals and in",
           lambda d: proc(d, "rescan_sensor_channels")["outcome"]["success"].update({"in": ["complete"]}))

    reject("a lost binding line, with nothing saying internal",
           lambda d: sig(d, "ventilation_level").pop("binding"))
    reject("both a binding and internal: true",
           lambda d: sig(d, "ventilation_level").update({"internal": True}))
    reject("internal: false written out",
           lambda d: sig(d, "nvm_write_count").update({"internal": False}))

    print("-- must stay legal")
    accept("uppercase enum member", lambda d: sig(d, "operating_mode")["enum"].update({9: "ECO"}))
    def bool_on_coil(d):
        d["signals"]["supply_fan_enable"] = {
            "title": "Supply Fan Enable", "kind": "setting", "type": "bool",
            "binding": {"space": "coil", "address": 5, "encoding": "bit"},
            "default": False,
        }

    def bool_as_bit(d):
        d["signals"]["supply_fan_running"] = {
            "title": "Supply Fan Running", "kind": "measurement", "type": "bool",
            "access": "read_only",
            "binding": {"space": "discrete", "address": 9, "encoding": "bit"},
        }

    def real_as_float(d):
        s = sig(d, "supply_ch1_temperature_level")
        s["binding"]["encoding"] = "float32"
        s.pop("scale")

    accept("a bool on a coil", bool_on_coil)
    accept("a bool on a discrete input", bool_as_bit)
    accept("the same real carried as float32 instead of scaled int16", real_as_float)
    accept("storage declared wider than the range needs",
           lambda d: sig(d, "ventilation_level").update({"storage": "uint32"}))
    accept("hidden service register", lambda d: sig(d, "internal_voltage_3v3").update({"hidden": True}))
    accept("internal property, marked as such (D8, D14)",
           lambda d: sig(d, "ventilation_level").pop("binding") and None or
                     sig(d, "ventilation_level").update({"internal": True}))
    accept("the same address number reused in a different space",
           lambda d: sig(d, "ventilation_level")["binding"].update({"space": "input"}))
    accept("an outcome keyed on a single error bit",
           lambda d: proc(d, "swap_air_chains")["outcome"].update(
               {"failure": {"signal": "error_memory_fault", "equals": True}}))
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
