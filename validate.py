#!/usr/bin/env python3
"""Validate a device profile: a properties document plus a Modbus register map.

Three layers.

Layer 1 is the JSON Schemas: shape and vocabulary of each document on its own.

Layer 2 is what a schema cannot express because it needs a whole document at
once -- reference resolution, enum coverage, storage widths, and D1's derived
reverse index.

Layer 3 is what neither document can check alone: the two must agree. Every
property that is not internal has to be carried by exactly one register field,
every field has to name a property that exists, no two fields may overlap, and a
field's encoding has to suit the property's type. This layer is why the D15
split costs nothing in safety -- the completeness a single file got from keeping
the address beside the property is recovered as a check.

Usage:  python validate.py [properties.yaml [modbus.yaml]]

Exit status is 1 if anything is an error, 0 if only warnings.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).parent
PROPS_DOC = HERE / "device-properties.yaml"
MODBUS_DOC = HERE / "device-modbus.yaml"
PROPS_SCHEMA = HERE / "device-properties.schema.json"
MODBUS_SCHEMA = HERE / "device-modbus.schema.json"

# Words an expression may use that are not property references. Provisional:
# the grammar is DECISIONS.md D3 and still open, so this list and the tokenizer
# below are a conservative approximation, not the real parser.
EXPR_KEYWORDS = {"and", "or", "not", "in", "any", "all", "true", "false", "null"}
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
WHEN_KEYS = ("present_when", "valid_when", "writable_when", "effective_when", "when")

# Which encoding can carry which semantic type. The point of D10's split is that
# this is a many-to-many table, not an identity.
ENCODINGS_FOR = {
    "bool":  {"bit", "uint16", "int16"},
    "int":   {"uint16", "int16", "uint32", "int32", "uint64", "int64"},
    "real":  {"float32", "float64", "uint16", "int16", "uint32", "int32"},
    "enum":  {"uint16", "uint32"},
    "text":  {"chars"},
}
WIDTH_CAPACITY = {
    "bool": (0, 1), "uint8": (0, 255), "int8": (-128, 127),
    "uint16": (0, 65535), "int16": (-32768, 32767),
    "uint32": (0, 4294967295), "int32": (-2147483648, 2147483647),
    "uint64": (0, 18446744073709551615),
    "int64": (-9223372036854775808, 9223372036854775807),
}
# How many registers one element of an encoding occupies.
SPAN = {"uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "float32": 2,
        "uint64": 4, "int64": 4, "float64": 4, "chars": 1}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def print(self) -> int:
        for m in self.errors:
            print(f"error: {m}")
        for m in self.warnings:
            print(f"warning: {m}")
        if not self.errors and not self.warnings:
            print("ok")
        else:
            print(f"\n{len(self.errors)} error(s), {len(self.warnings)} warning(s)")
        return 1 if self.errors else 0


def _key(k):
    if k is True:
        return "true"
    if k is False:
        return "false"
    return str(k)


def normalize(node):
    """Stringify enum and label keys before schema validation.

    YAML parses `0: none` as an integer key and `false: disabled` as a boolean
    one. JSON has neither, and a JSON Schema `propertyNames` pattern only
    constrains strings -- so left alone, those key rules silently pass
    anything. Canonicalising here makes them enforceable and matches what any
    JSON form of the documents will hold. Key types only.
    """
    if isinstance(node, dict):
        return {k: ({_key(bk): bv for bk, bv in v.items()}
                    if k in ("enum", "labels") and isinstance(v, dict) else normalize(v))
                for k, v in node.items()}
    if isinstance(node, list):
        return [normalize(v) for v in node]
    return node


def load_yaml(path: pathlib.Path):
    try:
        import yaml
    except ImportError:
        sys.exit("validate.py needs PyYAML: python -m pip install pyyaml jsonschema")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def check_schema(doc, schema_path: pathlib.Path, label: str, rep: Report) -> None:
    try:
        import jsonschema
    except ImportError:
        rep.warn("jsonschema not installed; skipped layer 1 (shape) entirely")
        return
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for err in sorted(validator.iter_errors(normalize(doc)), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in err.path) or "(root)"
        rep.error(f"{label}: {where}: {err.message}")


def walk(node, path=()):
    if isinstance(node, dict):
        for k, v in node.items():
            yield path, k, v
            yield from walk(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, path + (i,))


def expression_identifiers(expr: str) -> set[str]:
    """Identifiers an expression mentions, minus keywords and attribute tails.

    Approximate until D3 pins the grammar down: it drops anything after a dot,
    and cannot tell a property reference from a member name -- the caller
    resolves that.
    """
    stripped = re.sub(r"\.[A-Za-z_][A-Za-z0-9_]*", "", expr)
    return {m.group(0) for m in IDENT_RE.finditer(stripped)} - EXPR_KEYWORDS


def member_namespace(props):
    members: dict[str, set[str]] = {}
    for name, sig in props.items():
        got = set((sig.get("enum") or {}).values())
        got |= set((sig.get("labels") or {}).values())
        for v in sig.get("variants") or []:
            got |= set((v.get("enum") or {}).values())
        if got:
            members[name] = got
    return members


def property_expressions(sig):
    exprs = [sig[k] for k in ("present_when", "valid_when", "writable_when") if k in sig]
    exprs += [v[k] for v in sig.get("variants") or [] for k in WHEN_KEYS if k in v]
    exprs += [c[k] for c in sig.get("constraints") or [] for k in WHEN_KEYS if k in c]
    return exprs


# ------------------------------------------------------------------ layer 2
def check_references(doc, rep: Report) -> None:
    props = doc.get("properties") or {}
    procedures = doc.get("procedures") or {}
    enum_members = member_namespace(props)
    all_members = {m for ms in enum_members.values() for m in ms}

    def want(ref, where):
        if ref not in props:
            rep.error(f"{where}: property '{ref}' is referenced but never declared")

    for path, key, value in walk(doc):
        where = "/".join(str(p) for p in path + (key,))
        if key in ("property", "read", "enum_from") and isinstance(value, str):
            want(value, where)
            if key == "enum_from" and value in props and value not in enum_members:
                rep.error(f"{where}: '{value}' has no enum to take values from")
        elif key == "reread" and isinstance(value, list):
            for ref in value:
                want(ref, where)

    def idents_ok(expr, where):
        for ident in sorted(expression_identifiers(expr)):
            if ident not in props and ident not in all_members:
                rep.error(f"{where}: '{ident}' in expression is neither a property "
                          f"nor a member of one")

    for name, sig in props.items():
        for expr in property_expressions(sig):
            idents_ok(expr, f"properties/{name}")
    for pname, proc in procedures.items():
        for i, expr in enumerate(proc.get("preconditions") or []):
            idents_ok(expr, f"procedures/{pname}/preconditions/{i}")

    for name, sig in props.items():
        for group, entries in (("constraints", sig.get("constraints")),
                               ("variants", sig.get("variants"))):
            if not entries:
                continue
            mentioned: dict[str, set[str]] = {}
            for entry in entries:
                expr = entry.get("when") or entry.get("effective_when") or ""
                idents = expression_identifiers(expr)
                for sel in idents & set(enum_members):
                    mentioned.setdefault(sel, set())
                    mentioned[sel] |= idents & enum_members[sel]
            for sel, covered in mentioned.items():
                missing = enum_members[sel] - covered
                if missing:
                    rep.warn(f"properties/{name}/{group}: no entry covers "
                             f"{sel} == {', '.join(sorted(missing))}")

    for name, sig in props.items():
        if sig.get("kind") == "setting" and "default" not in sig:
            rep.warn(f"properties/{name}: a setting with no default cannot be "
                     f"factory-reset or offered a starting value")


def raw_span(sig):
    if "range" not in sig:
        return None
    scale = sig.get("scale", 1) or 1
    lo, hi = sig["range"]
    return (lo / scale, hi / scale)


def narrowest_width(lo: float, hi: float):
    for w in ("uint8", "int8", "uint16", "int16", "uint32", "int32", "uint64", "int64"):
        c = WIDTH_CAPACITY[w]
        if lo >= c[0] - 0.5 and hi <= c[1] + 0.5:
            return w
    return None


def check_storage(doc, encodings, rep: Report) -> None:
    """Storage width, derived from range and checked against any declaration."""
    for name, sig in (doc.get("properties") or {}).items():
        stype = sig.get("type")
        if stype is None:
            continue
        span = raw_span(sig)
        declared = sig.get("storage")
        derived = None
        if stype == "bool":
            derived = "bool"
        elif stype == "enum":
            top = max((int(v) for v in (sig.get("enum") or {})), default=0)
            derived = narrowest_width(0, top)
        elif span:
            derived = narrowest_width(*span)
        elif encodings.get(name) in WIDTH_CAPACITY:
            derived = encodings[name]

        if declared and declared in WIDTH_CAPACITY and span:
            c = WIDTH_CAPACITY[declared]
            if span[0] < c[0] - 0.5 or span[1] > c[1] + 0.5:
                rep.error(f"properties/{name}: declared storage '{declared}' cannot hold "
                          f"raw range {span[0]:.0f}..{span[1]:.0f}")
        elif not declared and derived is None and stype != "text":
            rep.error(f"properties/{name}: storage width is underivable -- give it a "
                      f"range, an encoding in the register map, or an explicit storage")

        if stype == "text" and "max_length" not in sig:
            rep.error(f"properties/{name}: type 'text' needs max_length")


def reverse_index(doc) -> dict[str, set[str]]:
    """D1's derived index: selector property -> properties that depend on it."""
    props = doc.get("properties") or {}
    index: dict[str, set[str]] = {}
    for name, sig in props.items():
        for expr in property_expressions(sig):
            for ident in expression_identifiers(expr):
                if ident in props and ident != name:
                    index.setdefault(ident, set()).add(name)
    return index


def check_reread_dependents(doc, index, rep: Report) -> None:
    for name, sig in (doc.get("properties") or {}).items():
        if (sig.get("on_write") or {}).get("reread") == "dependents" and not index.get(name):
            rep.warn(f"properties/{name}/on_write/reread: 'dependents' resolves to "
                     f"nothing -- no expression references {name}")


# ------------------------------------------------------------------ layer 3
def check_agreement(props_doc, modbus_doc, rep: Report):
    """The two documents have to agree. Returns (encoding per property, on-bus set)."""
    props = props_doc.get("properties") or {}
    registers = modbus_doc.get("registers") or []

    encodings: dict[str, str] = {}
    owner: dict[str, str] = {}        # property -> where it is carried
    whole: dict[tuple, str] = {}      # (space, address) -> what occupies it entirely
    packed: dict[tuple, str] = {}     # (space, address) -> the register packing bits
    bits: dict[tuple, str] = {}       # (space, address, bit) -> field

    def claim(space, addr, what, at, kind):
        key = (space, addr)
        prior = whole.get(key) or packed.get(key)
        if prior:
            rep.error(f"{at}: {space} {addr} is already used by {prior}")
        (whole if kind == "whole" else packed)[key] = what

    for ri, reg in enumerate(registers):
        space = reg.get("space")
        at = f"registers/{ri}"

        if reg.get("reserved"):
            lo, hi = reg.get("from"), reg.get("to")
            if lo is not None and hi is not None:
                if hi < lo:
                    rep.error(f"{at}: reserved range {lo}..{hi} runs backwards")
                for a in range(lo, hi + 1):
                    claim(space, a, f"a reserved range ({at})", at, "whole")
            continue

        addr = reg.get("address")
        fields = reg.get("fields") or []
        if any("bit" in f for f in fields):
            claim(space, addr, f"packed bits ({at})", at, "packed")

        for fi, field in enumerate(fields):
            fat = f"{at}/fields/{fi}"
            name = field.get("property")
            if name not in props:
                rep.error(f"{fat}: names property '{name}', which is not declared")
                continue
            if name in owner:
                rep.error(f"{fat}: property '{name}' is already carried by {owner[name]}")
                continue
            owner[name] = fat
            sig = props[name]
            stype = sig.get("type")

            if sig.get("internal"):
                rep.error(f"{fat}: '{name}' is marked internal but a register carries it")

            if "bit" in field:
                key = (space, addr, field["bit"])
                if key in bits:
                    rep.error(f"{fat}: bit {field['bit']} of {space} {addr} is already "
                              f"taken by {bits[key]}")
                bits[key] = fat
                encodings[name] = "bit"
                if stype != "bool":
                    rep.error(f"{fat}: '{name}' is type '{stype}', but only a bool can be "
                              f"carried as a single bit")
                continue

            enc = field.get("encoding")
            encodings[name] = enc
            span = SPAN.get(enc, 1) * field.get("count", 1)
            for a in range(addr, addr + span):
                claim(space, a, f"'{name}' ({fat})", fat, "whole")

            if stype and enc not in ENCODINGS_FOR.get(stype, set()):
                rep.error(f"{fat}: type '{stype}' cannot be carried as '{enc}' "
                          f"(allowed: {', '.join(sorted(ENCODINGS_FOR[stype]))})")
            elif stype == "real" and enc not in ("float32", "float64") and "scale" not in sig:
                rep.error(f"{fat}: a real carried as '{enc}' needs a scale, or its "
                          f"fractional part is unrepresentable")
            rs = raw_span(sig)
            if rs and enc in WIDTH_CAPACITY:
                c = WIDTH_CAPACITY[enc]
                if rs[0] < c[0] - 0.5 or rs[1] > c[1] + 0.5:
                    rep.error(f"{fat}: range {sig['range']} is raw {rs[0]:.0f}..{rs[1]:.0f} "
                              f"after scale, which does not fit '{enc}' ({c[0]}..{c[1]})")
            if stype == "enum" and enc in WIDTH_CAPACITY:
                top = max((int(v) for v in (sig.get("enum") or {})), default=0)
                if top > WIDTH_CAPACITY[enc][1]:
                    rep.error(f"{fat}: enum value {top} does not fit '{enc}'")

        # A register's access is derived from what it carries. An explicit one may
        # only narrow it: the bus may offer less than the property allows, never
        # more. Checking agreement instead of deriving would be D1's mistake --
        # two copies of one fact, with a checker standing in for a single source.
        declared = reg.get("access")
        if declared:
            allowed = {props[f["property"]].get("access", "read_write")
                       for f in fields if f.get("property") in props}
            derived = ("read_only" if allowed == {"read_only"}
                       else "write_only" if allowed == {"write_only"}
                       else "read_write")
            if derived != "read_write" and declared != derived:
                rep.error(f"{at}: access '{declared}' widens what its properties allow "
                          f"('{derived}'); a register may only narrow access")

    for name, sig in props.items():
        if not sig.get("internal") and name not in owner:
            rep.error(f"properties/{name}: carried by no register and not marked internal "
                      f"-- it would silently vanish from the register map")

    on_bus = set(owner)

    for name in sorted(on_bus):
        for expr in property_expressions(props[name]):
            for ident in sorted(expression_identifiers(expr)):
                if ident in props and ident not in on_bus:
                    rep.error(f"properties/{name}: condition references '{ident}', which "
                              f"no register carries -- a client cannot evaluate it")

    for pname, proc in (props_doc.get("procedures") or {}).items():
        steps = list(proc.get("steps") or []) + list(proc.get("on_failure") or [])
        for i, step in enumerate(steps):
            targets = []
            for verb in ("write", "read"):
                if isinstance(step.get(verb), dict) and "property" in step[verb]:
                    targets.append(step[verb]["property"])
            if isinstance(step.get("await"), dict) and "property" in step["await"]:
                targets.append(step["await"]["property"])
            targets += list(step.get("reread") or [])
            if isinstance(step.get("verify"), dict) and "read" in step["verify"]:
                targets.append(step["verify"]["read"])
            for ref in targets:
                if ref in props and ref not in on_bus:
                    rep.error(f"procedures/{pname}/steps/{i}: targets '{ref}', which no "
                              f"register carries, so a procedure cannot reach it")

    return encodings, on_bus


def main(argv: list[str]) -> int:
    props_path = pathlib.Path(argv[1]) if len(argv) > 1 else PROPS_DOC
    modbus_path = pathlib.Path(argv[2]) if len(argv) > 2 else MODBUS_DOC
    props_doc = load_yaml(props_path)
    modbus_doc = load_yaml(modbus_path)
    rep = Report()

    check_schema(props_doc, PROPS_SCHEMA, props_path.name, rep)
    check_schema(modbus_doc, MODBUS_SCHEMA, modbus_path.name, rep)
    check_references(props_doc, rep)
    encodings, _ = check_agreement(props_doc, modbus_doc, rep)
    check_storage(props_doc, encodings, rep)
    index = reverse_index(props_doc)
    check_reread_dependents(props_doc, index, rep)

    print(f"-- derived reverse index ({props_path.name})")
    if not index:
        print("   (empty)")
    for sel in sorted(index):
        print(f"   {sel} -> {', '.join(sorted(index[sel]))}")
    print()

    return rep.print()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
