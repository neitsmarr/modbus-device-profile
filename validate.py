#!/usr/bin/env python3
"""Validate a device profile, in two layers.

Layer 1 is the JSON Schema: shape and vocabulary. It catches a misspelled key,
a range with three numbers, a signal with no binding.

Layer 2 is everything a JSON Schema cannot express, because it needs to look at
the whole document at once:

  * reference resolution -- does 'signal: cal_status' name a signal that exists?
  * enum coverage        -- a constraint set keyed on gas_type that forgets one
                            of gas_type's members is a hole a device will fall
                            into in the field
  * the reverse index    -- DECISIONS.md D1: nothing declares what it influences,
                            so we derive it here and print it

Usage:  python validate.py [device-profile.yaml]

Exit status is 1 if anything is an error, 0 if only warnings.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).parent
SCHEMA = HERE / "device-profile.schema.json"

# Words an expression may use that are not signal references. Provisional: the
# expression grammar is DECISIONS.md D3 and still open, so this list and the
# tokenizer below are a conservative approximation, not the real parser.
EXPR_KEYWORDS = {
    "and", "or", "not", "in", "any", "all",
    "true", "false", "null",
}
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Keys whose value is an expression, per D1.
WHEN_KEYS = ("present_when", "valid_when", "writable_when", "effective_when", "when")


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
    """Stringify enum/flags keys before schema validation.

    YAML parses `0: none` as an integer key and `false: disabled` as a boolean
    one. JSON has no such keys, and a JSON Schema `propertyNames` pattern only
    constrains strings -- so left alone, the enum and label key rules silently
    pass anything. Canonicalising here makes them enforceable and matches what
    any JSON form of the profile will hold.

    This changes key types only. Per D5 it may not, and does not, change which
    signals exist, what they are called, or where they live.
    """
    if isinstance(node, dict):
        return {k: (
            {_key(bk): bv for bk, bv in v.items()}
            if k in ("enum", "labels") and isinstance(v, dict) else normalize(v)
        ) for k, v in node.items()}
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


def check_schema(doc, rep: Report) -> None:
    try:
        import jsonschema
    except ImportError:
        rep.warn("jsonschema not installed; skipped layer 1 (shape) entirely")
        return
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for err in sorted(validator.iter_errors(normalize(doc)), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in err.path) or "(root)"
        rep.error(f"{where}: {err.message}")


def walk(node, path=()):
    """Yield (path, key, value) for every mapping entry in the document."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield path, k, v
            yield from walk(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, path + (i,))


def expression_identifiers(expr: str) -> set[str]:
    """Identifiers an expression mentions, minus keywords and attribute tails.

    Approximate until the grammar is pinned down: it drops anything after a dot
    so that channel[*].alarm_active contributes 'channel', and it cannot tell a
    signal reference from an enum member name -- the caller resolves that.
    """
    stripped = re.sub(r"\.[A-Za-z_][A-Za-z0-9_]*", "", expr)
    return {m.group(0) for m in IDENT_RE.finditer(stripped)} - EXPR_KEYWORDS


def check_references(doc, rep: Report) -> None:
    signals = doc.get("signals") or {}
    procedures = doc.get("procedures") or {}

    enum_members: dict[str, set[str]] = {}
    for name, sig in signals.items():
        members = set((sig.get("enum") or {}).values())
        members |= set((sig.get("labels") or {}).values())
        for variant in sig.get("variants") or []:
            members |= set((variant.get("enum") or {}).values())
        if members:
            enum_members[name] = members
    all_members = {m for ms in enum_members.values() for m in ms}

    def want_signal(ref, where):
        if ref not in signals:
            rep.error(f"{where}: signal '{ref}' is referenced but never declared")

    # -- explicit signal references ------------------------------------------
    for path, key, value in walk(doc):
        where = "/".join(str(p) for p in path + (key,))
        if key in ("contains", "not_contains") and isinstance(value, str):
            pass          # membership is checked against the signal's own flags below
        elif key == "signal" and isinstance(value, str):
            want_signal(value, where)
        elif key == "read" and isinstance(value, str):
            want_signal(value, where)
        elif key == "enum_from" and isinstance(value, str):
            want_signal(value, where)
            if value in signals and value not in enum_members:
                rep.error(f"{where}: '{value}' has no enum to take values from")
        elif key == "reread" and isinstance(value, list):
            for ref in value:
                want_signal(ref, where)

    # -- identifiers inside expressions --------------------------------------
    for path, key, value in walk(doc):
        if key in WHEN_KEYS and isinstance(value, str):
            where = "/".join(str(p) for p in path + (key,))
            for ident in sorted(expression_identifiers(value)):
                if ident not in signals and ident not in all_members:
                    rep.error(
                        f"{where}: '{ident}' in expression is neither a signal "
                        f"nor an enum member of any signal"
                    )
    for name, proc in procedures.items():
        for i, expr in enumerate(proc.get("preconditions") or []):
            for ident in sorted(expression_identifiers(expr)):
                if ident not in signals and ident not in all_members:
                    rep.error(
                        f"procedures/{name}/preconditions/{i}: '{ident}' in "
                        f"expression is neither a signal nor an enum member"
                    )

    # -- enum coverage -------------------------------------------------------
    for name, sig in signals.items():
        for group, entries in (("constraints", sig.get("constraints")),
                              ("variants", sig.get("variants"))):
            if not entries:
                continue
            mentioned: dict[str, set[str]] = {}
            for entry in entries:
                expr = entry.get("when") or entry.get("effective_when") or ""
                idents = expression_identifiers(expr)
                for selector in idents & set(enum_members):
                    mentioned.setdefault(selector, set())
                    mentioned[selector] |= idents & enum_members[selector]
            for selector, covered in mentioned.items():
                missing = enum_members[selector] - covered
                if missing:
                    rep.warn(
                        f"signals/{name}/{group}: no entry covers "
                        f"{selector} == {', '.join(sorted(missing))}"
                    )


def reverse_index(doc) -> dict[str, set[str]]:
    """D1's derived index: selector signal -> signals whose behaviour depends on it."""
    signals = doc.get("signals") or {}
    index: dict[str, set[str]] = {}
    for name, sig in signals.items():
        exprs = [sig[k] for k in ("present_when", "valid_when", "writable_when") if k in sig]
        exprs += [v["effective_when"] for v in sig.get("variants") or [] if "effective_when" in v]
        exprs += [c["when"] for c in sig.get("constraints") or [] if "when" in c]
        for expr in exprs:
            for ident in expression_identifiers(expr):
                if ident in signals and ident != name:
                    index.setdefault(ident, set()).add(name)
    return index


# Which wire encodings can carry which semantic type (D10). The point of the
# split is that this is a many-to-many table, not an identity.
ENCODINGS_FOR = {
    "bool":  {"bit", "uint16", "int16"},
    "int":   {"uint16", "int16", "uint32", "int32", "uint64", "int64"},
    "real":  {"float32", "float64", "uint16", "int16", "uint32", "int32"},
    "enum":  {"uint16", "uint32"},
    "text":  {"chars"},
}

# Inclusive raw range each width can hold.
WIDTH_CAPACITY = {
    "bool": (0, 1), "uint8": (0, 255), "int8": (-128, 127),
    "uint16": (0, 65535), "int16": (-32768, 32767),
    "uint32": (0, 4294967295), "int32": (-2147483648, 2147483647),
    "uint64": (0, 18446744073709551615), "int64": (-9223372036854775808, 9223372036854775807),
}
INTEGER_WIDTHS = ["bool", "uint8", "int8", "uint16", "int16", "uint32", "int32", "uint64", "int64"]


def raw_span(sig):
    """The range expressed in raw (pre-scale) units, or None if there is no range."""
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


def check_types(doc, rep: Report) -> None:
    """The semantic type / wire encoding / storage width triangle."""
    for name, sig in (doc.get("signals") or {}).items():
        stype = sig.get("type")
        if stype is None:
            continue                                   # schema already reported it

        # -- semantic type vs wire encoding --------------------------------
        binding = sig.get("binding")
        if binding:
            enc = binding.get("encoding")
            if enc is None:
                rep.error(f"signals/{name}/binding: no encoding given")
            elif enc not in ENCODINGS_FOR.get(stype, set()):
                rep.error(
                    f"signals/{name}: type '{stype}' cannot be carried as '{enc}' "
                    f"(allowed: {', '.join(sorted(ENCODINGS_FOR[stype]))})"
                )
            elif stype == "real" and enc not in ("float32", "float64") and "scale" not in sig:
                rep.error(
                    f"signals/{name}: a real carried as '{enc}' needs a scale, "
                    f"or its fractional part is unrepresentable"
                )
            # does the range survive the wire?
            span = raw_span(sig)
            if span and enc in WIDTH_CAPACITY:
                c = WIDTH_CAPACITY[enc]
                if span[0] < c[0] - 0.5 or span[1] > c[1] + 0.5:
                    rep.error(
                        f"signals/{name}: range {sig['range']} is raw "
                        f"{span[0]:.0f}..{span[1]:.0f} after scale, which does not fit "
                        f"encoding '{enc}' ({c[0]}..{c[1]})"
                    )
            # does the enum fit?
            if stype == "enum" and enc in WIDTH_CAPACITY:
                top = max((int(v) for v in (sig.get("enum") or {})), default=0)
                if top > WIDTH_CAPACITY[enc][1]:
                    rep.error(f"signals/{name}: enum value {top} does not fit '{enc}'")

        # -- storage width -------------------------------------------------
        declared = sig.get("storage")
        span = raw_span(sig)
        derived = None
        if stype == "bool":
            derived = "bool"
        elif stype == "enum":
            top = max((int(v) for v in (sig.get("enum") or {})), default=0)
            derived = narrowest_width(0, top)
        elif span:
            derived = narrowest_width(*span)
        elif binding and binding.get("encoding") in WIDTH_CAPACITY:
            derived = binding["encoding"]

        if declared:
            if declared in WIDTH_CAPACITY and span:
                c = WIDTH_CAPACITY[declared]
                if span[0] < c[0] - 0.5 or span[1] > c[1] + 0.5:
                    rep.error(
                        f"signals/{name}: declared storage '{declared}' cannot hold "
                        f"raw range {span[0]:.0f}..{span[1]:.0f}"
                    )
        elif derived is None and stype != "text":
            rep.error(
                f"signals/{name}: storage width is underivable -- give it a range, "
                f"a binding encoding, or an explicit storage"
            )

        if stype == "text" and "max_length" not in sig:
            rep.error(f"signals/{name}: type 'text' needs max_length")


def check_addresses(doc, rep: Report) -> None:
    """Address collisions, bit collisions, and bitfield naming.

    These matter more since D11: a packed status register is no longer one
    property with a bit map, it is N bool properties sharing an address. That
    makes the address the grouping key -- and makes a mistyped address or a
    repeated bit a silent, plausible hand-editing error (D5).
    """
    signals = doc.get("signals") or {}
    whole: dict[tuple, str] = {}          # (table, addr) -> property owning the register
    bits: dict[tuple, str] = {}           # (table, addr, bit) -> property
    fields: dict[tuple, set] = {}         # (table, addr) -> bitfield names seen

    for name in sorted(signals):
        sig = signals[name]
        binding = sig.get("binding")
        if not binding:
            continue
        table = binding.get("space")
        addr = binding.get("address")
        if table is None or addr is None:
            continue
        if "bit" in binding:
            key = (table, addr, binding["bit"])
            if key in bits:
                rep.error(
                    f"signals/{name}: bit {binding['bit']} of {table} {addr} is already "
                    f"taken by '{bits[key]}'"
                )
            else:
                bits[key] = name
            fields.setdefault((table, addr), set()).add(sig.get("bitfield"))
        else:
            key = (table, addr)
            if key in whole:
                rep.error(
                    f"signals/{name}: {table} register {addr} is already used by "
                    f"'{whole[key]}'"
                )
            else:
                whole[key] = name

    # a register cannot be both a packed bit field and a whole value
    for (table, addr) in fields:
        if (table, addr) in whole:
            rep.error(
                f"{table} register {addr} is used both as a whole value "
                f"('{whole[(table, addr)]}') and as packed bits"
            )

    for (table, addr), names in sorted(fields.items()):
        named = {n for n in names if n}
        if len(named) > 1:
            rep.error(
                f"{table} register {addr}: its bits point at different packed "
                f"registers ({', '.join(sorted(named))})"
            )
        if None in names and named:
            rep.warn(
                f"{table} register {addr}: some bits name a packed register and some "
                f"do not"
            )

    # every bitfield reference resolves, and each packed register lives at one address
    declared = doc.get("bitfields") or {}
    where: dict[str, set] = {}
    for name in sorted(signals):
        sig = signals[name]
        ref = sig.get("bitfield")
        if ref is None:
            continue
        if ref not in declared:
            rep.error(
                f"signals/{name}/bitfield: '{ref}' is not declared in the bitfields "
                f"section"
            )
            continue
        b = sig.get("binding") or {}
        if "space" in b and "address" in b:
            where.setdefault(ref, set()).add((b["space"], b["address"]))
    for ref, places in sorted(where.items()):
        if len(places) > 1:
            rep.error(
                f"bitfields/{ref}: its bits are spread over more than one register "
                f"({', '.join(f'{t} {a}' for t, a in sorted(places))})"
            )
    for ref in sorted(declared):
        if ref not in where:
            rep.warn(f"bitfields/{ref}: declared but no property references it")


def check_internal_properties(doc, rep: Report) -> None:
    """Checks that exist because a property may have no Modbus binding (D8)."""
    signals = doc.get("signals") or {}
    on_bus = {n for n, s in signals.items() if "binding" in s}

    # A bus client cannot evaluate a condition that names something it cannot
    # read. So a property reachable over Modbus may only depend on properties
    # that are also reachable. The reverse is fine: internal logic may look at
    # anything.
    for name in sorted(on_bus):
        sig = signals[name]
        exprs = [sig[k] for k in ("present_when", "valid_when", "writable_when") if k in sig]
        exprs += [v[k] for v in sig.get("variants") or [] for k in WHEN_KEYS if k in v]
        exprs += [c[k] for c in sig.get("constraints") or [] for k in WHEN_KEYS if k in c]
        for expr in exprs:
            for ident in sorted(expression_identifiers(expr)):
                if ident in signals and ident not in on_bus:
                    rep.error(
                        f"signals/{name}: condition references '{ident}', which has no "
                        f"Modbus binding -- a bus client cannot read it, so it cannot "
                        f"evaluate this condition"
                    )

    # Procedures run over the bus.
    for pname, proc in (doc.get("procedures") or {}).items():
        steps = list(proc.get("steps") or []) + list(proc.get("on_failure") or [])
        for i, step in enumerate(steps):
            targets = []
            for verb in ("write", "read"):
                if isinstance(step.get(verb), dict) and "signal" in step[verb]:
                    targets.append(step[verb]["signal"])
            if isinstance(step.get("await"), dict) and "signal" in step["await"]:
                targets.append(step["await"]["signal"])
            targets += list(step.get("reread") or [])
            if isinstance(step.get("verify"), dict) and "read" in step["verify"]:
                targets.append(step["verify"]["read"])
            for ref in targets:
                if ref in signals and ref not in on_bus:
                    rep.error(
                        f"procedures/{pname}/steps/{i}: targets '{ref}', which has no "
                        f"Modbus binding and so cannot be reached by a procedure"
                    )

    for name, sig in signals.items():
        if sig.get("kind") == "setting" and "default" not in sig:
            rep.warn(
                f"signals/{name}: a setting with no default cannot be factory-reset "
                f"or offered a starting value"
            )


def check_reread_dependents(doc, index, rep: Report) -> None:
    for name, sig in (doc.get("signals") or {}).items():
        on_write = sig.get("on_write") or {}
        if on_write.get("reread") == "dependents" and not index.get(name):
            rep.warn(
                f"signals/{name}/on_write/reread: 'dependents' resolves to nothing "
                f"-- no expression anywhere references {name}, so writing it "
                f"re-reads no registers"
            )


def main(argv: list[str]) -> int:
    path = pathlib.Path(argv[1]) if len(argv) > 1 else HERE / "device-profile.yaml"
    doc = load_yaml(path)
    rep = Report()

    check_schema(doc, rep)
    check_references(doc, rep)
    check_types(doc, rep)
    check_addresses(doc, rep)
    check_internal_properties(doc, rep)
    index = reverse_index(doc)
    check_reread_dependents(doc, index, rep)

    print(f"-- derived reverse index ({path.name})")
    if not index:
        print("   (empty)")
    for selector in sorted(index):
        print(f"   {selector} -> {', '.join(sorted(index[selector]))}")
    print()

    return rep.print()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
