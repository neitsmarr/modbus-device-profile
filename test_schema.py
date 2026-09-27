#!/usr/bin/env python3
"""Negative tests for device-profile.schema.json.

A schema that accepts everything passes the happy path too, so the only way to
know it is doing work is to feed it mutations of a known-good profile and check
each one is rejected. Every REJECT case below is a mistake someone will
plausibly make while hand-editing a profile.

Usage:  python test_schema.py
"""

from __future__ import annotations

import copy
import io
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).parent


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

    baseline = list(validator.iter_errors(base))
    if baseline:
        failures.append(f"baseline profile does not satisfy its own schema ({len(baseline)} errors)")
        for err in baseline[:5]:
            print("   baseline:", "/".join(str(p) for p in err.path), err.message)

    def check(expect: str, name: str, mutate) -> None:
        doc = copy.deepcopy(base)
        mutate(doc)
        n = len(list(validator.iter_errors(doc)))
        ok = (n > 0) if expect == "reject" else (n == 0)
        print(f"   {'pass' if ok else 'FAIL'}  {expect}: {name}")
        if not ok:
            failures.append(f"{expect}: {name}")

    def reject(name, mutate):
        check("reject", name, mutate)

    def accept(name, mutate):
        check("accept", name, mutate)

    sig = lambda d, n: d["signals"][n]
    proc = lambda d, n: d["procedures"][n]

    print("-- vocabulary")
    reject("misspelled key (writeable_when)", lambda d: sig(d, "humidity").update({"writeable_when": "x"}))
    reject("unknown top-level section", lambda d: d.update({"signal": {}}))
    reject("CamelCase signal name", lambda d: d["signals"].update({"GasType": sig(d, "gas_type")}))
    reject("unknown data type", lambda d: sig(d, "humidity")["binding"].update({"type": "uint12"}))

    print("-- bindings")
    reject("signal with no binding", lambda d: sig(d, "alarm_level_1").pop("binding"))
    reject("two tables in one binding", lambda d: sig(d, "humidity")["binding"].update({"holding": 5}))
    reject("address above 65535", lambda d: sig(d, "humidity")["binding"].update({"input": 70000}))
    reject("bit without type bool", lambda d: sig(d, "humidity")["binding"].update({"bit": 3}))
    reject("function code not in Modbus", lambda d: d["access"]["supported_fc"].append(99))

    print("-- presentation")
    reject("range with three numbers", lambda d: sig(d, "alarm_level_1")["constraints"][0].update({"range": [1, 2, 3]}))
    reject("scale of zero", lambda d: sig(d, "alarm_level_1").update({"scale": 0}))
    reject("variants plus top-level unit", lambda d: sig(d, "humidity").update({"unit": "%RH"}))
    reject("a single variant", lambda d: sig(d, "humidity").__setitem__("variants", [sig(d, "humidity")["variants"][0]]))
    reject("constraint with when and nothing else", lambda d: sig(d, "alarm_level_1")["constraints"].append({"when": "gas_type == propane"}))

    print("-- procedures")
    reject("confirm without danger", lambda d: proc(d, "set_gas_type").pop("danger"))
    reject("procedure with no steps", lambda d: proc(d, "set_gas_type").pop("steps"))
    reject("await without timeout", lambda d: proc(d, "zero_calibrate")["steps"][1]["await"].pop("timeout_ms"))
    reject("step with two verbs", lambda d: proc(d, "zero_calibrate")["steps"][0].update({"delay_ms": 5}))
    reject("duplicate entries in reread", lambda d: proc(d, "set_gas_type")["steps"][2].update({"reread": ["alarm_level_1", "alarm_level_1"]}))

    print("-- must stay legal")
    accept("registers section", lambda d: d.update({"registers": {"unlock": {"binding": {"holding": 1}, "title": "Write lock"}}}))
    accept("uppercase enum member", lambda d: sig(d, "gas_type")["enum"].update({3: "CNG"}))
    accept("bit flag as bool", lambda d: sig(d, "humidity")["binding"].update({"bit": 3, "type": "bool"}))
    accept("signal gated by present_when", lambda d: sig(d, "alarm_level_1").update({"present_when": "gas_type == methane"}))

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
