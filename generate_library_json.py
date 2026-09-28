#!/usr/bin/env python3
"""Generate a 3SModbus DeviceLibrary profile from the two documents.

This is the first actual consumer of the format, and it exists to answer a
fair objection: the register map looks a lot like the JSON profiles the company
already has. It does, because it describes the same thing. The difference is
that a library profile repeats the semantics into every register -- units,
decoder ranges, defaults -- while the register map references properties that
hold them once.

So the relationship is superset, not rivalry: these two documents can produce
the existing format, which means adoption does not require rewriting 3SModbus
or the 25 profiles it reads.

The output is validated against the library's own schemas when they are
reachable. What does not survive the conversion is reported, because that is
the honest measure of what the richer model buys.

Usage:  python generate_library_json.py [outfile.json]
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).parent
# Where a 3SModbus DeviceLibrary checkout lives, used only to validate the
# generated profile against its own schemas. Override with DEVICE_LIBRARY;
# validation is skipped when it is not reachable, so this is never required.
LIBRARY = pathlib.Path(os.environ.get(
    "DEVICE_LIBRARY",
    r"..\software\sw_3smcenter\3SModbus\DeviceLibrary"))

ACCESS_OUT = {"read_only": "readOnly", "write_only": "writeOnly",
              "read_write": "readWrite"}


def load(path):
    import yaml
    with io.open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def bit_label(title: str) -> str:
    """'Device Status - Errors: Supply Voltage Fault' -> 'Supply Voltage Fault'."""
    return title.split(": ", 1)[1] if ": " in title else title


def numerical(sig):
    dec = {"type": "numerical", "multiplier": sig.get("scale", 1)}
    if "range" in sig:
        dec["minimum"], dec["maximum"] = sig["range"]
    if sig.get("decimals"):
        dec["decimalPlaces"] = sig["decimals"]
    step = sig.get("step")
    if isinstance(step, int):
        dec["increment"] = step
    return dec


def decoder_for(space, reg, fields, props, unsupported):
    """A library decoder for one register."""
    if any("bit" in f for f in fields):
        bits = [{"index": f["bit"], "label": bit_label(f.get("title") or f["property"])}
                for f in sorted(fields, key=lambda f: f["bit"])]
        clear = reg.get("clear_label")
        if not clear:
            unsupported.append(f"{space} {reg['address']}: no clear_label, so the "
                               f"library's required 'no bit set' label must be invented")
            clear = "OK"
        return {"type": "bitwise", "default": clear, "bits": bits}

    sig = props[fields[0]["property"]]
    stype = sig.get("type")
    if stype == "enum":
        return {"type": "dictionary",
                "entries": [{"value": int(v), "label": n}
                            for v, n in sorted(sig["enum"].items(), key=lambda kv: int(kv[0]))]}
    if stype == "bool":
        lab = sig.get("labels") or {}
        return {"type": "dictionary",
                "entries": [{"value": 0, "label": str(lab.get(False, "false"))},
                            {"value": 1, "label": str(lab.get(True, "true"))}]}
    if stype == "text":
        return {"type": "bytewise", "byteEncoding": "unicode"}
    if sig.get("display") == "version":
        return {"type": "bytewise", "byteEncoding": "hexadecimal",
                "separator": ".", "trimStart": "0"}
    if sig.get("display") == "hex":
        return {"type": "bytewise", "byteEncoding": "hexadecimal"}
    return numerical(sig)


SIMPLE = re.compile(r"^\s*(\w+)\s*(==|!=)\s*(\w+)\s*$")
BARE = re.compile(r"^\s*(not\s+)?(\w+)\s*$")


def condition_for(sig, addr_of, props, unsupported, name):
    """Translate a presence condition into the library's one-comparison form.

    Their grammar is `[register] [operator] [value]` -- a single comparison
    against a decoded label. Anything with a conjunction, a negation or a bare
    boolean cannot be carried across, which is most of what this device needs.
    """
    expr = sig.get("present_when") or sig.get("valid_when")
    if not expr:
        return None
    bare = BARE.match(expr)
    if bare:
        # A bare boolean that is a bit of a bitwise register translates to their
        # membership test against the bit's label.
        negated, ref = bare.groups()
        if ref in addr_of and props.get(ref, {}).get("type") == "bool":
            space, addr = addr_of[ref]
            tag = "IR" if space == "input" else "HR"
            label = bit_label(props[ref].get("title") or ref)
            op = "!contains" if negated else "contains"
            return f"{tag}{addr} {op} {label}"
        unsupported.append(f"{name}: bare condition '{expr}' has no register to test")
        return None
    m = SIMPLE.match(expr)
    if not m:
        unsupported.append(f"{name}: condition needs more than one comparison: {expr}")
        return None
    ref, op, value = m.groups()
    if ref not in addr_of:
        unsupported.append(f"{name}: condition references '{ref}', which has no register")
        return None
    space, addr = addr_of[ref]
    tag = "IR" if space == "input" else "HR"
    other = props[ref]
    if other.get("type") in ("enum", "bool"):
        op = "contains" if op == "==" else "!contains"
    return f"{tag}{addr} {op} {value}"


def main(argv):
    modbus_doc = load(HERE / "device-modbus.yaml")
    dev = modbus_doc["device"]

    # Since D21 a field carries its own meaning, so the lookup the semantic
    # document used to provide is built from the map itself.
    props = {f["property"]: f
             for block in modbus_doc["spaces"].values()
             for reg in block["registers"]
             for f in reg.get("fields") or []}

    # Spaces are containers (D20), so walk them rather than one flat list.
    registers = [(space, block["base"], reg)
                 for space, block in modbus_doc["spaces"].items()
                 for reg in block["registers"]]

    addr_of = {}
    for space, _base, reg in registers:
        for f in reg.get("fields") or []:
            addr_of[f["property"]] = (space, reg["address"])

    unsupported: list[str] = []
    out = {"deviceName": dev["model"],
           "deviceDescription": dev.get("description", ""),
           "deviceType": dev["type"],
           "firmwareVersion": dev["firmware_version"]["from"],
           "inputRegisters": [], "holdingRegisters": []}

    for space, base, reg in registers:
        fields = reg.get("fields") or []
        if not fields:
            unsupported.append(f"{space} {reg.get('from')}..{reg.get('to')}: "
                               f"reserved range has no representation in the library format")
            continue
        sig = fields[0]
        name = reg.get("title") or sig.get("title") or sig["property"]
        # The library numbers registers from 1, so go via the wire offset rather
        # than copying our address across: the two only coincide when the space
        # happens to declare a base of 1 (D17).
        entry = {"number": reg["address"] - base + 1, "name": name,
                 "decoder": decoder_for(space, reg, fields, props, unsupported)}

        if len(fields) == 1 and "bit" not in fields[0]:
            if sig.get("unit"):
                entry["units"] = sig["unit"]
            if sig.get("hidden"):
                entry["hidden"] = True
            if space == "holding":
                if "default" in sig:
                    entry["default"] = str(sig["default"])
                acc = reg.get("access", "read_write")
                if acc != "read_write":
                    entry["access"] = ACCESS_OUT[acc]
            cond = condition_for(sig, addr_of, props, unsupported, fields[0]["property"])
            if cond:
                entry["condition"] = cond

        key = "inputRegisters" if space == "input" else "holdingRegisters"
        out[key].append(entry)

    dest = pathlib.Path(argv[1]) if len(argv) > 1 else HERE / "generated-library-profile.json"
    io.open(dest, "w", encoding="utf-8", newline="\n").write(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n")

    print(f"wrote {dest.name}: {len(out['holdingRegisters'])} holding, "
          f"{len(out['inputRegisters'])} input registers")

    # -- validate against the library's own schemas, if they are reachable ----
    schema_dir = LIBRARY / "Schemas"
    if schema_dir.is_dir():
        try:
            import jsonschema
            from referencing import Registry, Resource
            res = []
            for f in schema_dir.glob("*.schema.json"):
                doc = json.loads(f.read_text(encoding="utf-8"))
                res.append(("./" + f.name, Resource.from_contents(doc)))
                res.append((doc.get("$id", f.name), Resource.from_contents(doc)))
            registry = Registry().with_resources(res)
            main_schema = json.loads(
                (schema_dir / "DeviceProfile.schema.json").read_text(encoding="utf-8"))
            v = jsonschema.Draft7Validator(main_schema, registry=registry)
            errs = list(v.iter_errors(out))
            print(f"validated against the library's own schema: {len(errs)} errors")
            for e in errs[:8]:
                print("   ", "/".join(str(p) for p in e.path), "|", e.message[:110])
        except ImportError as exc:
            print(f"(skipped library-schema validation: {exc})")
    else:
        print("(library schemas not reachable from here; skipped)")

    print(f"\nwhat does not survive the conversion: {len(unsupported)}")
    seen = set()
    for u in unsupported:
        kind = u.split(":", 1)[1].strip()[:60]
        if kind not in seen:
            seen.add(kind)
            print("   ", u[:150])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
