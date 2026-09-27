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
