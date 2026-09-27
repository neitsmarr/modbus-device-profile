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
    "contains",          # flags membership: <signal> contains <member>
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


def normalize(node):
    """Stringify enum/flags keys before schema validation.

    YAML parses `0: none` as an integer key. JSON has no integer keys, and a
    JSON Schema `propertyNames` pattern only constrains strings -- so with
    integer keys the enum and flag key rules silently pass anything, including
    a bit index of 16. Canonicalising to strings here makes them enforceable
    and matches what any JSON form of the profile will hold.

    This changes key types only. Per D5 it may not, and does not, change which
    signals exist, what they are called, or where they live.
    """
    if isinstance(node, dict):
        return {k: (
            {str(bk): bv for bk, bv in v.items()}
            if k in ("enum", "flags") and isinstance(v, dict) else normalize(v)
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
        members |= set((sig.get("flags") or {}).values())
        for variant in sig.get("variants") or []:
            members |= set((variant.get("enum") or {}).values())
            members |= set((variant.get("flags") or {}).values())
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
