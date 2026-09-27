# Design decisions

Newest last. Each entry is the rule, why, and what it costs.

## D1 — Dependencies are declared once, on the dependent signal

**Rule.** A dependency between signals is written exactly once, in the
declaration of the signal that is *affected*. The declaring keys are:

| Key                  | Question it answers                                  |
|----------------------|------------------------------------------------------|
| `present_when`       | Does this signal exist in this configuration at all? |
| `valid_when`         | Is the value currently meaningful to read?           |
| `writable_when`      | May a client write it right now?                     |
| `effective_when`     | Which of several interpretations applies?            |
| `constraints[].when` | Which range / default / step applies?                |

Nothing declares its influence on other signals. There is no `selects:`,
no `affects:`, no `invalidates:`.

**Why.** Two-sided declarations are two facts that must agree, and nothing
in the file format can make them agree. `gas_type.selects` and
`alarm_level_1.constraints[].when` encode the same edge; the moment one is
edited without the other the profile is silently wrong, and the three
consumers (docs, commissioning tool, firmware X-macro) disagree about which
one is authoritative. Declaring on the dependent side also puts the
dependency next to the thing it constrains, which is where a firmware author
editing a register needs to see it.

**The reverse direction is derived.** "What does `gas_type` influence?" is a
real question — the commissioning tool needs it to know what to re-read
after a write, and the docs generator needs it to render a
"changing this affects…" note. It is answered by an index built from the
parsed file: for every signal, for every expression in the keys above,
collect the signal identifiers referenced and add a reverse edge. One pass,
a handful of lines, and it can never be out of sync because there is nothing
to keep in sync.

**Consequence for `on_write.reread`.** `reread: dependents` stays, and now
means precisely "the reverse-index entry for this signal" rather than
"the list the author remembered to type". An explicit list remains allowed
for the case where the device re-clamps something the expressions do not
mention.

**Cost.** Expressions are now load-bearing: the reverse index is only as
good as the expression parser, so the expression grammar has to be small,
total, and specified — not an escape hatch for arbitrary host code. That is
a constraint worth accepting, since the firmware target needs the same
property anyway.

## D2 — Validation is two layers: a JSON Schema for shape, a script for meaning

**Rule.** `device-profile.schema.json` (JSON Schema 2020-12) is authoritative for
structure and vocabulary. `validate.py` adds the checks that need the whole
document at once. A profile is valid only when both pass.

**Why split.** A JSON Schema is a tree-local predicate: it can say "range is two
numbers", it cannot say "`cal_status` names a signal that exists" or "these
constraints cover every member of `gas_type`". Those need the document as a
graph. Trying to force them into the schema produces unreadable `$dynamicRef`
contortions that still do not cover the cases; keeping them in a script that
reads the parsed document is both shorter and honest about what it is doing.

The split lands where it should: the schema is what an editor can enforce while
you type (a `# yaml-language-server: $schema=` modeline is in the profile, so
VS Code flags a misspelled key immediately), and the script is what CI runs.

**The schema is strict.** `additionalProperties: false` everywhere. For a file
that three consumers read as ground truth, a key nobody understands is worse
than a rejected file — `writeable_when` should not silently mean nothing.

**Invariants worth naming**, beyond types:

- `variants` present forbids top-level `unit`/`scale`/`range`/`enum` on the
  same signal. Two interpretations with no precedence rule is a bug.
- `variants` needs at least two entries. One variant is a signal with a
  needless condition attached.
- A `constraints` entry needs `when` *and* at least one of
  `range`/`default`/`step`, or it constrains nothing.
- A step has exactly one verb; `confirm` requires `danger`.
- `bit` requires `type: bool`.

`test_schema.py` holds these as negative cases, since a schema that accepts
everything also passes the happy path. Each case is a mistake a human editing
a profile by hand will actually make.

**Vocabulary this forced into the open.** Writing the schema required naming
things the source profile used without declaring. Each is provisional and
marked so in the schema:

- `registers:` — a new top-level section for named raw registers that are not
  signals (`unlock`, `cmd`). Procedures already wrote to them; nothing declared
  them. Without this, `binding: unlock` in a step cannot be checked at all.
- `access:` on a signal is a privilege ladder (`user` / `commissioning` /
  `factory`). The source only ever used `commissioning`, so two thirds of this
  is invented and needs confirming.
- The `data_type` list, and `byte_order` / `word_order` for multi-register
  values — unavoidable once a type is an enum rather than a free string.
- Enum member names may be uppercase (`LPG`), signal names may not. Forcing
  `lpg` to satisfy one pattern was the wrong trade: acronyms read wrong
  lowercased and C enum constants are conventionally uppercase.

**What this does not fix.** Layer 2 currently reports eleven unresolved
references in the profile — `unlock`, `cmd`, `cal_status`, `alarm_level_2`,
`device_mode`, `channel` — because the sketch references signals and registers
it never declares. These are gaps in the profile, not in the schema, and
closing them needs register addresses that only the device author has.

## D3 — Expression grammar (OPEN)

Not decided. D1 made expressions load-bearing: the reverse index is only as
good as what can be extracted from them. `validate.py` currently approximates
with a regex that strips attribute tails and subtracts a keyword list, which is
enough to build the index for the expressions in hand and not enough to be
trusted.

What needs settling: comparison operators; `and` / `or` / `not`; enum member
literals; numeric literals with or without units; and the quantifier form the
preconditions already use — `any(channel[*].alarm_active)` — which implies the
format models repeated channels, and it currently does not. The grammar must
stay small and total, because the firmware X-macro target needs to evaluate it
without a heap or a parser generator.
