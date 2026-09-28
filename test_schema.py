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
    def hr(d):
        return d["spaces"]["holding"]["registers"]

    def coil(d, reg):
        d["spaces"]["coil"] = {"base": 1, "registers": [reg]}

    def reg_of(d, name):
        for block in d["spaces"].values():
            for reg in block["registers"]:
                for f in reg.get("fields") or []:
                    if f.get("property") == name:
                        return reg
        raise AssertionError(name)

    suite("device-modbus.yaml", "device-modbus.schema.json", [
        ("reject", "no spaces section", lambda d: d.pop("spaces")),
        ("reject", "a space with no registers", lambda d: d["spaces"]["holding"].pop("registers")),
        ("reject", "a space with no base", lambda d: d["spaces"]["holding"].pop("base")),
        ("reject", "an address space Modbus does not have",
         lambda d: d["spaces"].update({"register": {"base": 1, "registers": [{"address": 1}]}})),
        ("reject", "a register still carrying its own space",
         lambda d: hr(d)[0].update({"space": "holding"})),
        ("reject", "register with neither address nor reserved range", lambda d: hr(d)[0].pop("address")),
        # An address merely past the end of its space is now a validate.py error
        # rather than a schema one: the window depends on addressing[space], and
        # a schema cannot see a sibling section. What stays here is the absurd.
        ("reject", "an address no numbering scheme could reach",
         lambda d: hr(d)[0].update({"address": 2_000_000})),
        ("reject", "register carrying nothing and not reserved", lambda d: hr(d)[0].pop("fields")),
        ("reject", "reserved range that also carries fields",
         lambda d: hr(d).append({"from": 7, "to": 8,
                                          "reserved": "spare",
                                          "fields": [{"property": "device_type", "encoding": "uint16"}]})),
        ("reject", "reserved range with no prose saying why",
         lambda d: hr(d).append({"from": 7, "to": 8})),
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
        ("reject", "unknown key on a register", lambda d: hr(d)[0].update({"decoder": "numerical"})),
        ("reject", "function code not in Modbus", lambda d: d["limits"]["supported_fc"].append(99)),
        ("reject", "max_read_words above the Modbus limit", lambda d: d["limits"].update({"max_read_words": 200})),
        ("accept", "a single-field register with no title of its own", lambda d: None),
        ("accept", "a reserved range with prose",
         lambda d: hr(d).append({"from": 7, "to": 8,
                                          "reserved": "Reserved for future interface settings."})),
        ("accept", "a 32-bit value in one field",
         lambda d: d["spaces"]["input"]["registers"].append({"address": 600,
                                          "fields": [{"property": "nvm_write_count", "encoding": "uint32"}]})),
        ("accept", "a string spanning registers",
         lambda d: d["spaces"]["input"]["registers"].append({"address": 610, "title": "Serial Number",
                                          "fields": [{"property": "serial_number",
                                                      "encoding": "chars", "count": 8}]})),
        # ---- A1: each space states what its numbering counts from
        ("reject", "an empty spaces section", lambda d: d.update({"spaces": {}})),
        ("reject", "a base that is not a number",
         lambda d: d["spaces"]["holding"].update({"base": "data_model"})),
        ("reject", "a negative base", lambda d: d["spaces"]["holding"].update({"base": -1})),
        ("reject", "an unknown key on a space",
         lambda d: d["spaces"]["holding"].update({"access": "read_write"})),
        ("accept", "raw wire offsets", lambda d: d["spaces"]["holding"].update({"base": 0})),
        ("accept", "a space numbered differently from the rest",
         lambda d: d["spaces"]["input"].update({"base": 0})),
        ("accept", "legacy Modicon numbering, transcribed as printed",
         lambda d: (d["spaces"]["holding"].update({"base": 40001}),
                    [r.update({"address": r["address"] + 40000})
                     for r in hr(d) if "address" in r],
                    [r.update({"from": r["from"] + 40000, "to": r["to"] + 40000})
                     for r in hr(d) if "from" in r],
                    [p.update({"address": p["address"] + 40000})
                     for p in d["identification"]["registers"] if p["space"] == "holding"])),

        # ---- A2: a coil is a single bit, which since D20 is its shape and not a rule
        ("accept", "a coil naming the one property it carries",
         lambda d: coil(d, {"address": 5, "property": "supply_fan_enable"})),
        ("accept", "a reserved range of coils",
         lambda d: coil(d, {"from": 10, "to": 20, "reserved": "Spare."})),
        ("reject", "a coil choosing an encoding, when a coil is one bit",
         lambda d: coil(d, {"address": 5, "property": "supply_fan_enable",
                            "encoding": "uint16"})),
        ("reject", "a coil with a bit position, when the address already is one",
         lambda d: coil(d, {"address": 5, "property": "supply_fan_enable", "bit": 0})),
        ("reject", "a coil with a fields list, when it carries exactly one property",
         lambda d: coil(d, {"address": 5, "title": "Two",
                            "fields": [{"property": "a"}, {"property": "b"}]})),
        ("reject", "a coil carrying nothing and not reserved", lambda d: coil(d, {"address": 5})),
        ("reject", "a discrete input carrying a float32",
         lambda d: d["spaces"].update({"discrete": {"base": 1, "registers": [
             {"address": 6, "property": "mcu_temperature", "encoding": "float32"}]}})),
        ("reject", "a clear_label on a coil, where no set of bits can be clear",
         lambda d: coil(d, {"address": 5, "property": "supply_fan_enable", "clear_label": "OK"})),
        ("reject", "encoding 'bit' in a 16-bit register, which says nothing about which bit",
         lambda d: reg_of(d, "device_type")["fields"][0].update({"encoding": "bit"})),
        ("reject", "count on an encoding with no elements",
         lambda d: reg_of(d, "device_type")["fields"][0].update({"count": 4})),

        # ---- A4: which device this is
        ("reject", "no device section", lambda d: d.pop("device")),
        ("reject", "a device with no vendor", lambda d: d["device"].pop("vendor")),
        ("reject", "a device with no model", lambda d: d["device"].pop("model")),
        ("reject", "a firmware range with no lower bound",
         lambda d: d["device"].update({"firmware_version": {"to": "2.0"}})),
        ("reject", "a firmware version that is not major.minor",
         lambda d: d["device"].update({"firmware_version": {"from": "v1"}})),
        ("accept", "a closed firmware range",
         lambda d: d["device"].update({"firmware_version": {"from": "1.0", "to": "1.4"}})),

        # ---- identification
        ("reject", "a probe with no expectation at all",
         lambda d: d["identification"]["registers"].append({"space": "holding", "address": 4})),
        ("reject", "a probe expecting two different things",
         lambda d: d["identification"]["registers"].append(
             {"space": "holding", "address": 4, "equals": 1, "in": [1, 2]})),
        ("reject", "an identification that identifies nothing", lambda d: d["identification"].clear()),
        ("reject", "an unknown device-id object",
         lambda d: d["identification"]["read_device_id"].update({"serial_number": "x"})),
        ("accept", "a probe matching a family of product ids",
         lambda d: d["identification"]["registers"].append(
             {"space": "holding", "address": 7, "in": [4010, 4011, 4012]})),

        # ---- FC 43 / Read Device Identification
        ("accept", "declaring FC 43 and FC 17 as supported",
         lambda d: d["limits"].update({"supported_fc": [3, 4, 6, 16, 17, 43]})),
        ("reject", "an unknown conformity level",
         lambda d: d["identification"]["read_device_id"].update({"conformity_level": 4})),
        ("accept", "extended conformity with individual object access",
         lambda d: d["identification"]["read_device_id"].update({"conformity_level": 131})),
        ("reject", "an FC 43 object that is not one of the defined ones",
         lambda d: d["identification"]["read_device_id"].update({"user_application_name": "x"})),
        ("reject", "an empty vendor name from FC 43",
         lambda d: d["identification"]["read_device_id"].update({"vendor_name": ""})),
        ("accept", "identification by FC 43 alone, with no register probe",
         lambda d: d["identification"].pop("registers")),

        # ---- exceptions
        ("reject", "an exception with no code", lambda d: d["exceptions"][0].pop("code")),
        ("reject", "exception code 0", lambda d: d["exceptions"][0].update({"code": 0})),
        ("reject", "an exception code that does not fit a byte",
         lambda d: d["exceptions"][0].update({"code": 256})),
        ("reject", "an exception with no short name", lambda d: d["exceptions"][0].pop("name")),
        ("reject", "an exception with no description",
         lambda d: d["exceptions"][0].pop("description")),
        ("reject", "an empty short name", lambda d: d["exceptions"][0].update({"name": ""})),
        ("reject", "a short name too long to display inline",
         lambda d: d["exceptions"][0].update({"name": "A" * 41})),
        ("accept", "a short name at the length of the longest standard one",
         lambda d: d["exceptions"][0].update({"name": "Gateway Target Device Failed To Respond"})),
        ("accept", "a proprietary code carrying both texts",
         lambda d: d["exceptions"].append({"code": 0x55, "name": "Something",
                                           "description": "Explained here."})),
        ("reject", "overloads written as false rather than omitted",
         lambda d: d["exceptions"][0].update({"overloads": False})),
        ("accept", "a standard code marked as overloaded",
         lambda d: d["exceptions"][0].update({"overloads": True, "name": "Block Not Mapped",
                                              "description": "Means something else here."})),
        ("reject", "raised_by, which was removed",
         lambda d: d["exceptions"][0].update({"raised_by": [3, 4]})),
        ("reject", "an unknown key on an exception",
         lambda d: d["exceptions"][0].update({"severity": "high"})),
        ("reject", "an empty exceptions list", lambda d: d.update({"exceptions": []})),
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
