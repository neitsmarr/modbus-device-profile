#!/usr/bin/env python3
"""Negative tests for both schemas.

A schema that accepts everything passes the happy path too, so the only way to
know these are doing work is to mutate known-good documents and check each
mutation is rejected. Every 'reject' case is a mistake someone will plausibly
make while hand-editing, which per D5 is how these documents get written.

Cross-document agreement is not tested here -- it is not expressible in a
schema. See check_agreement in validate.py.

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

    failures: list[str] = []

    def suite(doc_name, schema_name, cases):
        schema = json.loads((HERE / schema_name).read_text(encoding="utf-8"))
        with io.open(HERE / doc_name, encoding="utf-8") as fh:
            base = yaml.safe_load(fh)
        validator = jsonschema.Draft202012Validator(schema)

        print(f"== {doc_name}")
        baseline = list(validator.iter_errors(normalize(base)))
        if baseline:
            failures.append(f"{doc_name} does not satisfy its own schema "
                            f"({len(baseline)} errors)")
            for err in baseline[:6]:
                print("   baseline:", "/".join(str(p) for p in err.path), err.message[:110])

        for expect, name, mutate in cases:
            doc = copy.deepcopy(base)
            mutate(doc)
            n = len(list(validator.iter_errors(normalize(doc))))
            ok = (n > 0) if expect == "reject" else (n == 0)
            print(f"   {'pass' if ok else 'FAIL'}  {expect}: {name}")
            if not ok:
                failures.append(f"{doc_name} {expect}: {name}")

    # ------------------------------------------------------------ properties
    p = lambda d, n: d["properties"][n]
    proc = lambda d, n: d["procedures"][n]

    def use_variants(d):
        s = p(d, "supply_ch1_voc_level")
        for k in ("unit", "scale", "range", "decimals"):
            s.pop(k, None)
        s["variants"] = [
            {"effective_when": "operating_mode == manual", "unit": "index"},
            {"effective_when": "operating_mode == automatic", "unit": "ppb"},
        ]

    suite("device-properties.yaml", "device-properties.schema.json", [
        ("reject", "missing device section", lambda d: d.pop("device")),
        ("reject", "device without firmware_version", lambda d: d["device"].pop("firmware_version")),
        ("reject", "firmware_version not major.minor", lambda d: d["device"].update({"firmware_version": "v1"})),
        ("reject", "an address leaking into the semantic document",
         lambda d: p(d, "ventilation_level").update({"binding": {"space": "holding", "address": 23}})),
        ("reject", "misspelled key (writeable_when)", lambda d: p(d, "ventilation_level").update({"writeable_when": "x"})),
        ("reject", "unknown top-level section", lambda d: d.update({"signals": {}})),
        ("reject", "CamelCase property name", lambda d: d["properties"].update({"FanSpeed": p(d, "supply_fan_speed")})),
        ("reject", "property with no kind", lambda d: p(d, "ventilation_level").pop("kind")),
        ("reject", "property with no semantic type", lambda d: p(d, "ventilation_level").pop("type")),
        ("reject", "invented access level", lambda d: p(d, "ventilation_level").update({"access": "commissioning"})),
        ("reject", "command that is not write_only", lambda d: p(d, "device_reset").update({"access": "read_write"})),
        ("reject", "measurement with a factory default", lambda d: p(d, "supply_fan_speed").update({"default": 0})),
        ("reject", "type enum without an enum map", lambda d: p(d, "operating_mode").pop("enum")),
        ("reject", "enum map on a non-enum type", lambda d: p(d, "ventilation_level").update({"enum": {0: "off"}})),
        ("reject", "labels on something that is not a bool",
         lambda d: p(d, "operating_mode").update({"labels": {"false": "a", "true": "b"}})),
        ("reject", "bool labels missing the true state", lambda d: p(d, "client_enable").update({"labels": {"false": "disabled"}})),
        ("reject", "scale on an enum", lambda d: p(d, "operating_mode").update({"scale": 0.1})),
        ("reject", "decimals on a bool", lambda d: p(d, "client_enable").update({"decimals": 1})),
        ("reject", "max_length on a non-text property", lambda d: p(d, "ventilation_level").update({"max_length": 8})),
        ("reject", "text without max_length", lambda d: p(d, "ventilation_level").update({"type": "text"})),
        ("reject", "enum member with a space", lambda d: p(d, "operating_mode")["enum"].update({9: "not valid"})),
        ("reject", "range with three numbers", lambda d: p(d, "ventilation_level").update({"range": [1, 2, 3]})),
        ("reject", "scale of zero", lambda d: p(d, "mcu_temperature").update({"scale": 0})),
        ("reject", "storage width that is not a real width", lambda d: p(d, "ventilation_level").update({"storage": "int24"})),
        ("reject", "internal: false written out", lambda d: p(d, "nvm_write_count").update({"internal": False})),
        ("reject", "constraint with when and nothing else",
         lambda d: p(d, "ventilation_level").update({"constraints": [{"when": "operating_mode == manual"}]})),
        ("reject", "confirm without danger", lambda d: proc(d, "swap_air_chains").pop("danger")),
        ("reject", "procedure with no steps", lambda d: proc(d, "swap_air_chains").pop("steps")),
        ("reject", "await without timeout", lambda d: proc(d, "rescan_sensor_channels")["steps"][1]["await"].pop("timeout_ms")),
        ("reject", "step with two verbs", lambda d: proc(d, "swap_air_chains")["steps"][2].update({"delay_ms": 5})),
        ("reject", "write step without a value", lambda d: proc(d, "swap_air_chains")["steps"][0]["write"].pop("value")),
        ("reject", "duplicate entries in reread",
         lambda d: proc(d, "rescan_sensor_channels")["steps"][2].update(
             {"reread": ["supply_ch1_provides_temperature"] * 2})),
        ("accept", "uppercase enum member", lambda d: p(d, "operating_mode")["enum"].update({9: "ECO"})),
        ("accept", "hidden service register", lambda d: p(d, "internal_voltage_3v3").update({"hidden": True})),
        ("accept", "an internal property", lambda d: p(d, "ventilation_level").update({"internal": True})),
        ("accept", "variants instead of a fixed unit", use_variants),
        ("accept", "a text property with max_length",
         lambda d: d["properties"].update({"serial_number": {
             "title": "Serial Number", "kind": "measurement", "type": "text",
             "access": "read_only", "max_length": 16}})),
    ])

    # ---------------------------------------------------------------- modbus
    def reg_of(d, name):
        for reg in d["registers"]:
            for f in reg.get("fields") or []:
                if f.get("property") == name:
                    return reg
        raise AssertionError(name)

    suite("device-modbus.yaml", "device-modbus.schema.json", [
        ("reject", "no registers section", lambda d: d.pop("registers")),
        ("reject", "register without a space", lambda d: d["registers"][0].pop("space")),
        ("reject", "unknown address space", lambda d: d["registers"][0].update({"space": "register"})),
        ("reject", "register with neither address nor reserved range", lambda d: d["registers"][0].pop("address")),
        ("reject", "address above 65535", lambda d: d["registers"][0].update({"address": 70000})),
        ("reject", "register carrying nothing and not reserved", lambda d: d["registers"][0].pop("fields")),
        ("reject", "reserved range that also carries fields",
         lambda d: d["registers"].append({"space": "holding", "from": 7, "to": 8,
                                          "reserved": "spare",
                                          "fields": [{"property": "device_type", "encoding": "uint16"}]})),
        ("reject", "reserved range with no prose saying why",
         lambda d: d["registers"].append({"space": "holding", "from": 7, "to": 8})),
        ("reject", "multi-field register with no title of its own",
         lambda d: reg_of(d, "error_memory_fault").pop("title")),
        ("reject", "field with both an encoding and a bit",
         lambda d: reg_of(d, "error_memory_fault")["fields"][0].update({"encoding": "uint16"})),
        ("reject", "field with neither encoding nor bit",
         lambda d: reg_of(d, "device_type")["fields"][0].pop("encoding")),
        ("reject", "bit position above 15",
         lambda d: reg_of(d, "error_memory_fault")["fields"][0].update({"bit": 20})),
        ("reject", "unknown encoding", lambda d: reg_of(d, "device_type")["fields"][0].update({"encoding": "uint12"})),
        ("reject", "field naming no property", lambda d: reg_of(d, "device_type")["fields"][0].pop("property")),
        ("reject", "unknown key on a register", lambda d: d["registers"][0].update({"decoder": "numerical"})),
        ("reject", "function code not in Modbus", lambda d: d["limits"]["supported_fc"].append(99)),
        ("reject", "max_read_words above the Modbus limit", lambda d: d["limits"].update({"max_read_words": 200})),
        ("accept", "a single-field register with no title of its own", lambda d: None),
        ("accept", "a reserved range with prose",
         lambda d: d["registers"].append({"space": "holding", "from": 7, "to": 8,
                                          "reserved": "Reserved for future interface settings."})),
        ("accept", "a 32-bit value in one field",
         lambda d: d["registers"].append({"space": "input", "address": 600,
                                          "fields": [{"property": "nvm_write_count", "encoding": "uint32"}]})),
        ("accept", "a string spanning registers",
         lambda d: d["registers"].append({"space": "input", "address": 610, "title": "Serial Number",
                                          "fields": [{"property": "serial_number",
                                                      "encoding": "chars", "count": 8}]})),
        ("accept", "a register on a coil",
         lambda d: d["registers"].append({"space": "coil", "address": 5,
                                          "fields": [{"property": "supply_fan_enable", "bit": 0}]})),
    ])

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
