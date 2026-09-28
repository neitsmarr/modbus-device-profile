#!/usr/bin/env python3
"""Negative tests for the register map schema.

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

    def hr(d):
        return d["spaces"]["holding"]["registers"]

    def coil(d, reg):
        if "reserved" not in reg:
            reg.setdefault("access", "read_write")
        d["spaces"]["coil"] = {"base": 1, "registers": [reg]}

    def reg_of(d, name):
        for block in d["spaces"].values():
            for reg in block["registers"]:
                for f in reg.get("fields") or []:
                    if f.get("id") == name:
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
                                          "fields": [{"id": "device_type", "encoding": "uint16"}]})),
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
        ("reject", "a field with no identifier", lambda d: reg_of(d, "device_type")["fields"][0].pop("id")),
        ("reject", "unknown key on a register", lambda d: hr(d)[0].update({"decoder": "numerical"})),
        ("reject", "function code not in Modbus", lambda d: d["limits"]["supported_fc"].append(99)),
        ("reject", "max_read_words above the Modbus limit", lambda d: d["limits"].update({"max_read_words": 200})),
        ("accept", "a single-field register with no title of its own", lambda d: None),
        ("accept", "a reserved range with prose",
         lambda d: hr(d).append({"from": 7, "to": 8,
                                          "reserved": "Reserved for future interface settings."})),
        ("accept", "a 32-bit value in one field",
         lambda d: d["spaces"]["input"]["registers"].append({"address": 600,
                                          "fields": [{"id": "nvm_write_count", "encoding": "uint32",
                                                      "type": "int"}]})),
        ("accept", "a string spanning registers",
         lambda d: d["spaces"]["input"]["registers"].append({"address": 610, "title": "Serial Number",
                                          "fields": [{"id": "serial_number", "type": "text",
                                                      "encoding": "chars", "count": 8,
                                                      "max_length": 16}]})),
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
         lambda d: coil(d, {"address": 5, "id": "supply_fan_enable"})),
        ("accept", "a reserved range of coils",
         lambda d: coil(d, {"from": 10, "to": 20, "reserved": "Spare."})),
        ("reject", "a coil choosing an encoding, when a coil is one bit",
         lambda d: coil(d, {"address": 5, "id": "supply_fan_enable",
                            "encoding": "uint16"})),
        ("reject", "a coil with a bit position, when the address already is one",
         lambda d: coil(d, {"address": 5, "id": "supply_fan_enable", "bit": 0})),
        ("reject", "a coil with a fields list, when it carries exactly one property",
         lambda d: coil(d, {"address": 5, "title": "Two",
                            "fields": [{"id": "a"}, {"id": "b"}]})),
        ("reject", "a coil carrying nothing and not reserved", lambda d: coil(d, {"address": 5})),
        ("reject", "a discrete input carrying a float32",
         lambda d: d["spaces"].update({"discrete": {"base": 1, "registers": [
             {"address": 6, "id": "mcu_temperature", "encoding": "float32"}]}})),
        ("reject", "a clear_label on a coil, where no set of bits can be clear",
         lambda d: coil(d, {"address": 5, "id": "supply_fan_enable", "clear_label": "OK"})),
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

        # ---- D21: a field now carries what the value means
        ("reject", "a field with no semantic type",
         lambda d: reg_of(d, "device_type")["fields"][0].pop("type")),
        ("reject", "a semantic type that is not one of the five",
         lambda d: reg_of(d, "device_type")["fields"][0].update({"type": "word"})),
        ("reject", "a scale of zero",
         lambda d: reg_of(d, "supply_voltage")["fields"][0].update({"scale": 0})),
        ("reject", "a range with three numbers",
         lambda d: reg_of(d, "ventilation_level")["fields"][0].update({"range": [1, 2, 3]})),
        ("reject", "an enum member with a space",
         lambda d: reg_of(d, "operating_mode")["fields"][0]["enum"].update({9: "not valid"})),
        ("reject", "bool labels missing the true state",
         lambda d: reg_of(d, "client_enable")["fields"][0].update({"labels": {"false": "off"}})),
        ("reject", "a misspelled condition key",
         lambda d: reg_of(d, "ventilation_level")["fields"][0].update({"writeable_when": "x"})),
        ("reject", "a constraint with a condition and nothing else",
         lambda d: reg_of(d, "ventilation_level")["fields"][0].update(
             {"constraints": [{"when": "operating_mode == manual"}]})),
        ("reject", "a single variant, which is a condition with nothing to choose between",
         lambda d: reg_of(d, "supply_voltage")["fields"][0].update(
             {"variants": [{"effective_when": "operating_mode == manual", "unit": "V"}]})),
        ("accept", "an uppercase enum member",
         lambda d: reg_of(d, "operating_mode")["fields"][0]["enum"].update({9: "ECO"})),
        ("accept", "a hidden service register",
         lambda d: reg_of(d, "internal_voltage_3v3").update({"hidden": True})),

        # ---- bit spans: several values of different widths in one register
        ("accept", "a settings word packing a flag, two enums and an integer",
         lambda d: hr(d).append({
             "address": 700, "title": "Communication Settings", "access": "read_write",
             "fields": [
                 {"id": "transmission_mode", "bit": 0, "type": "bool",
                  "title": "Transmission Mode", "labels": {"false": "rtu", "true": "ascii"}},
                 {"id": "cs_baud_rate", "bits": [1, 4], "type": "enum", "title": "Baud Rate",
                  "enum": {0: "b4800", 1: "b9600", 2: "b19200", 3: "b38400"}},
                 {"id": "cs_parity", "bits": [5, 6], "type": "enum", "title": "Parity",
                  "enum": {0: "none_8n1", 1: "even_8e1", 2: "odd_8o1"}},
                 {"id": "cs_timeout", "bits": [7, 14], "type": "int", "title": "Response Timeout",
                  "unit": "ms", "scale": 10, "range": [0, 2550]},
                 {"id": "cs_exception_first", "bit": 15, "type": "bool",
                  "title": "Send Exception Before Timeout"},
             ]})),
        ("reject", "a span with one bound",
         lambda d: reg_of(d, "error_memory_fault")["fields"][0].update({"bits": [3]})),
        ("reject", "a span past the top of a register",
         lambda d: reg_of(d, "error_memory_fault")["fields"][0].update({"bits": [14, 17]})),
        ("reject", "a field with both a bit and a span",
         lambda d: reg_of(d, "error_memory_fault")["fields"][0].update({"bits": [2, 3]})),
        ("reject", "a field with both a span and an encoding",
         lambda d: reg_of(d, "device_type")["fields"][0].update({"bits": [0, 3]})),
        ("reject", "a clear_label on a register packing an integer",
         lambda d: hr(d).append({
             "address": 701, "title": "Mixed", "access": "read_write", "clear_label": "OK",
             "fields": [{"id": "mixed_int", "bits": [0, 7], "type": "int", "title": "Count"}]})),

        # ---- access, which the specification leaves to the vendor per address
        ("reject", "a holding register that does not state its access",
         lambda d: hr(d)[0].pop("access")),
        ("reject", "an access value that is not one of the three",
         lambda d: hr(d)[0].update({"access": "readOnly"})),
        ("accept", "a write-only holding register",
         lambda d: hr(d)[0].update({"access": "write_only"})),
        ("accept", "a reserved holding range, which has no access to state",
         lambda d: hr(d).append({"from": 7, "to": 8, "reserved": "Spare."})),

        # ---- procedures moved across with the operations they are made of
        ("reject", "a procedure with no steps",
         lambda d: d["procedures"]["swap_air_chains"].pop("steps")),
        ("reject", "confirm without a danger level",
         lambda d: d["procedures"]["swap_air_chains"].pop("danger")),
        ("reject", "a step with two verbs",
         lambda d: d["procedures"]["swap_air_chains"]["steps"][2].update({"delay_ms": 5})),
        ("reject", "a write step with no value",
         lambda d: d["procedures"]["swap_air_chains"]["steps"][0]["write"].pop("value")),
        ("reject", "await without a timeout",
         lambda d: d["procedures"]["rescan_sensor_channels"]["steps"][1]["await"].pop("timeout_ms")),
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
