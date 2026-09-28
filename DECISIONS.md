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

## D4 — No channel dimension. Repetition, if any, is pre-expansion

**Decided: channels are not a dimension of the data model.** There are no 2D
signals, no per-channel scoping in expressions, no `any`/`all` quantifiers, no
stride as a semantic concept. A signal is a signal, identified by one name.

**Why not.** The device range is 1–2 channels for most products, 3–4 for some,
fixed in every case; only the current product nests (2 ventilation lines ×
3 channels, sensor per channel selected at runtime). A dimension would cost
expression scoping and quantifiers — the bulk of D3's difficulty — to serve a
handful of instances. Real devices also break the model it assumes: channel 1
parked at a legacy address for backward compatibility, holding registers
blocked while input registers interleave, and per-channel flags packed as bits
of a single register, which a stride cannot express at all. And since every
device profile is unique, a dimension earns nothing across profiles.

**The real cost of going flat is not typing.** Measured on the current product,
flat is 169 lines, 18 signals, 48 expression occurrences encoding 4 rules —
twelve hand-maintained copies of each rule. That is survivable. What is not
survivable is that *"channel 2" appears nowhere in a flat profile*, so nothing
can check that a channel-2 signal references channel-2 siblings. See the trap
below.

**YAML anchors do not solve this, and are worse than writing it out.** An
anchor can only share an identical subtree, but every instance's expressions
name instance-specific siblings. Aliasing a `constraints` block from channel 1
into channel 2 yields:

    ch1:  when: sensor_type_supply_1 == co2
    ch2:  when: sensor_type_supply_1 == co2    # aliased -- wrong channel

`validate.py` passes this clean: `sensor_type_supply_1` is a real signal, so
reference resolution and enum coverage both succeed. The device picks alarm
limits from the wrong channel's sensor and nothing anywhere says so. No check
can catch it without knowing which group a signal belongs to, which a flat
profile does not record.

So: anchors are fine for instance-independent values -- `enum` maps, `unit`,
`scale`, `confirm` prose. They are forbidden for any subtree containing an
expression. Worth a lint.

**Consequence for repetition.** Hand-maintained flat is the default and is
correct for 1–2 channel products. Where repetition is worth expressing, it goes
in as a `repeat` block that expands to flat signals *in the loader, before the
schema runs* -- not as a dimension. Substitution generates sibling names
mechanically, which makes the anchor bug class unrepresentable, and expansion
can emit a `group` / `index` label per signal, which is what makes a
"references its own group" lint possible at all and lets the docs generator
collapse 18 rows back into a formula.

The cost stays in one function: docs, commissioning tool and the firmware
X-macro all consume the expanded profile and need no changes, and D3's grammar
is untouched because `$line` / `$n` are textual substitution, not scoping.
Expansion must record provenance, or errors will point at generated names that
appear in no file. It is opt-in per group: if two groups turn out to have
different signal sets or irregular addresses, writing them out beats forcing
them through one `repeat`.

**Status.** The no-dimension half is decided. Whether to add `repeat` at all,
or keep even the 2×3 product fully hand-written, is still open — pending a
concrete syntax sketch to judge rather than argue about.

## D5 — No repeat blocks. The profile is readable without executing anything

**Decided.** D4 left open whether the nested product should get a pre-expansion
`repeat` block. It should not. Signals are written out, one declaration each,
by hand. This resolves D4 fully: no channel dimension and no repetition
construct.

**What the sketch showed.** A working `repeat` implementation for the 2x3
product came to 68 lines of YAML against 169 hand-written, with a 62-line
loader pass. The line count was never the problem:

- The design needed an `overrides` section to express its first realistic
  example — the supply line's first channel keeping a legacy address. An
  abstraction that requires an escape hatch immediately is not describing the
  domain, it is fighting it. Real register maps are irregular; a construct
  whose premise is regularity will be in permanent tension with them.
- Override merge semantics had to be specified and were wrong on the first
  attempt: a shallow merge silently dropped `type: uint16` from an overridden
  binding. That class of bug is permanent, not a one-off.
- It was hard to read. To answer "what registers does this device have?" you
  had to simulate the expansion in your head — two nested loops, a base
  summed per table, then textual substitution. For a file whose entire purpose
  is to be the one place the answer lives, that is disqualifying, independent
  of every other cost.

**The general rule this establishes.** The profile must be readable without
executing anything. No pass may change which signals exist, what they are
called, or where they live. A reader with the file and no tooling can always
enumerate the register map by eye. Deriving *indexes* over what is written is
fine and expected (D1's reverse index); *generating* what is written is not.

This is the criterion to apply to the open expression grammar (D3) as well: an
expression may be evaluated, but it may never conjure a signal.

**Reversible, deliberately.** The reasoning above is about cost, not
impossibility, and `expand.py` is 62 lines. If a future product makes the
duplication genuinely painful — the threshold is probably well past the 18
signals and 12 rule copies measured here — this is worth reopening. The record
exists so that decision starts from what was already learned rather than from
scratch.

**What survives.** Nothing about repeat, but the sketch established that the
cross-instance lint depends only on a signal knowing which instance it belongs
to, not on anything generating it. That is tracked separately as D6.

## D6 — Adopt the company library's vocabulary; add flags; drop the invented parts

The profile now describes the real product: a residential AHU with a Modbus
server interface and a Modbus client interface, two air chains, one EC fan per
chain (analog output or Modbus), up to three I2C sensor channels per chain, one
auxiliary digital input per chain, and left-right swap.

Naming and value conventions come from `3SModbus/DeviceLibrary`, which is the
authority here — it already encodes years of decisions about what these
registers are called. Adopted from it:

- the HR1–HR10 common block: slave address, baud rate, parity, device type,
  hardware and firmware version, termination resistor, registers reset
- `<Measurand> Level`, `<Measurand> Correction Value`, `Sensor State`,
  `Alerts` as OK / yellow / red, `Minimum`/`Maximum Output Value`,
  `Start Output Value`, kickstart control and time, overwrite enable and value
- output type as 0–10 VDC / 0–20 mA / PWM 12 V / PWM open collector
- the IR501+ diagnostics block: MCU temperature, lifetime, internal voltages
- units as written there: °C, %RH, ppm, hPa, %, VDC, rpm, seconds, minutes, bps
- a `device` section, because a profile is one firmware version's register map

**`flags` is new, and the product forced it.** A sensor channel can carry
several measurands at once, so channel capability is not a choice among
alternatives — it is a set. That needs a bit field with membership tests, which
the library also has (its `bitwise` decoder), so `flags:` maps bit index to
member name and expressions test it with `contains` / `not contains`. `enum`
and `flags` are mutually exclusive on one signal: a register is either
exclusive states or independent bits. Device status words, fan state and sensor
state all became `flags` too, which is what they always were.

**Two provisional things from D2 are now resolved, both by deletion:**

- The `registers:` section is gone. It existed because the old sketch wrote to
  `binding: unlock` and `binding: cmd` with nothing declaring them. The library
  has no such concept: a command is an ordinary holding register with
  `access: write_only` and a dictionary of states, like `Modbus Registers
  Reset: idle | reset`. Procedure steps now target signals only.
- The invented `user` / `commissioning` / `factory` access ladder is gone,
  replaced by the library's `read_only` / `write_only` / `read_write`, with
  `hidden` for service registers. Privilege was never what `access` meant.

Also added: `default` at signal level (every library register has one),
`decimals` for display precision (their `decimalPlaces`; `scale` already fixes
the value), and `not_contains` alongside `equals` / `in` in conditions.

**A hole this exposed in D2's schema.** YAML parses `0: none` as an *integer*
key, and a JSON Schema `propertyNames` pattern only constrains strings — so
every enum and flag key rule was silently vacuous, and a flag bit index of 16
validated cleanly. `validate.py` now canonicalises those keys to strings before
validation, which is also the form any JSON rendering of the profile will have.
It changes key types only, never what D5 protects.

**Scale, measured.** 173 signals, 1693 lines, 107 holding and 66 input
registers, four procedures. This is the first real test of D5: the six sensor
channels are six near-identical 18-signal blocks, written out. If that becomes
intolerable to maintain by hand, D5 is where to reopen the argument — the
numbers to weigh it against are in D4.

**Open, and needing the device author:**

- Every address is invented. The block layout imitates the library's style
  (decades per subsystem, 20 apart per channel, IR501+ diagnostics) but nothing
  is real.
- Bypass, preheater/reheater, heat-recovery efficiency and filter monitoring
  are not modelled. They were not in the hardware list, but a residential AHU
  normally has them, and the industry Modbus maps for comparable units all
  expose them.
- Hardware and firmware version are single `uint16` registers here. The library
  decodes them bytewise as hex. The format has no way to say that yet.
- `access.gaps_readable: true` and the read/write word limits are guesses.
- The client interface is modelled as configuration only. What it polls on the
  far side is another device's profile, and how one profile references another
  is not decided.

## D7 — Pre-heater, bypass, efficiency and filters

Added, now that they are confirmed: an optional pre-heater, a modulating
(analog) bypass, calculated heat recovery efficiency, and filter monitoring.
209 signals, 130 holding and 79 input registers, six procedures, 2095 lines.

**The pre-heater is the first hardware option, and `present_when` earns itself.**
`preheater_installed` is the single register the whole block hangs off: on a
unit without the heater, the other nine registers are *absent*, not idle. Every
one carries `present_when: preheater_installed == installed`, and the derived
index reports the six-way fan-out, so a commissioning tool re-reads the block
after the option is set. This is the shape every future option should take: one
declaring register, presence conditions on the dependents, nothing listing what
it controls.

**Efficiency exposed a real gap: a channel knows what it measures, not where it
is.** Heat recovery efficiency is (supply − outdoor) / (extract − outdoor), so
it needs four named points in the air path, and nothing in the profile said
which of the six sensor channels sits at each. Two ways to fix it:

- assign a role to each channel, then have efficiency ask "which channel is the
  outdoor one?" — a quantifier over channels, which D5 rules out; or
- have each air path point name its source channel.

The second, adopted, is also the library's own `Output N Source` pattern, so it
is not a new idea in this codebase. Four `*_temperature_source` selectors, four
derived temperatures each `valid_when` its source is assigned, and efficiency
`valid_when` all three of its inputs are. Flat, no quantifiers, four short
expressions instead of an eighteen-term disjunction.

It leaves one check no expression here can make: that the channel a source
names actually reports temperature — its `sensor_capability` must contain
`temperature`. That is a cross-signal, second-order condition (a property of
the signal a *value* points at, not of a signal an expression names), and it
belongs in the semantic layer, not the format. Noted in the profile beside the
selectors.

**Bypass is modulating, so it is an output, not a switch.** It gets the same
vocabulary as the fans — output type, minimum and maximum output value,
position feedback, state flags — plus the automatic-mode thresholds. Its state
includes `held_closed_by_missing_temperature`, because automatic bypass silently
depends on the air path sources above.

**Filters: both candidate mechanisms are modelled, neither is chosen.** That
filters get monitored is settled; how is not. `filter_monitoring_mode` selects
`elapsed_time` or `differential_pressure`, and each mechanism's registers are
present only under its own mode, so the decision can be made later without
reshaping the map.

This is marked PROVISIONAL in the profile for a concrete reason, not caution:
differential pressure needs a sensor the hardware list does not contain. The
I2C channels measure *barometric* pressure, not differential, so the
differential-pressure branch presumes hardware that may not exist. Deciding
filters therefore means deciding hardware first. If it lands on elapsed time,
delete the three differential registers and the mode enum collapses to a
boolean.

**Status words grew** rather than new alarm registers being invented:
`preheater_fault` and `bypass_fault` join `device_status_errors`,
`filter_replacement_due` and `efficiency_below_expected` join the warnings.

**Still open after this:** every address remains invented; reheater is not
modelled (only the pre-heater was confirmed); and the cross-check that a
temperature source names a temperature-capable channel needs writing into
`validate.py`.

## D8 — The profile describes device properties, not Modbus registers

**Decided.** The profile is a description of the device's properties. The Modbus
binding is one attribute of a property, and a property may have none. A property
with no binding exists on the device but is not on the bus.

**Why this was nearly free.** Measured before changing anything: 83% of the
average property body was already transport-neutral. Across 209 signals the only
Modbus-specific content was the address, the raw `type`, and `scale`. Everything
that carries meaning -- `unit`, `range`, `default`, `enum`, `flags`, `decimals`,
`access`, and all of `present_when` / `valid_when` / `writable_when` /
`constraints` / `variants` -- never mentioned a transport. The separation existed
already; this decision only names it and allows the binding to be absent.

Nothing in the conditional layer changed. Expressions, the D1 reverse index,
constraints and procedures do not reference transports, so they were untouched.

**Scope: persisted, published, or invocable.** A property belongs in the profile
if it has a lifetime beyond one control cycle and an identity someone outside
the firmware could ask about — settings, live data, commands. Working state does
not: loop counters, filter accumulators, PID intermediates, scratch.

Without that line the profile becomes a second copy of the firmware's internals,
which must then track code that changes every sprint. That is D1's "two facts
that must agree, and nothing can make them agree", at the scale of the whole
firmware instead of one dependency edge.

With it, the benefit holds even though an internal setting serves only *one*
consumer: firmware generates one table from one source, instead of a generated
table plus a parallel hand-maintained mechanism for the unexposed half. One
mechanism per concept is worth it on its own.

**`kind` is new, and the scope change forced it.** `setting` is persisted and has
a factory default; `measurement` is produced by the device and is volatile;
`command` is invoked and stores nothing. This is exactly what the three
generators switch on. It must be explicit rather than inferred, because
inference relied on which Modbus table a register sat in plus its access — and
an internal property has no table. The existing 209 classified as 124 settings,
83 measurements, 4 commands. The schema now enforces that a command is
`write_only` and a measurement is `read_only` with no default.

**Two checks became possible, and necessary.** Both exist only because a
property can now be off-bus, and both were verified to fire:

- A property that *is* on the bus may only be conditioned on properties that are
  also on the bus. A client cannot evaluate `present_when` naming something it
  cannot read. The reverse is fine: internal logic may look at anything.
- A procedure step may only target a property with a binding. Procedures run
  over the wire.

**Deliberately not done: multi-transport.** A second transport (CAN, KNX) is not
realistically planned, so the binding stays a single `binding:` rather than a map
keyed by transport. The shape does not preclude adding one later.

The encoding restructure that would prepare for it -- moving `scale`, raw
`type`, and the raw values of `enum` / `flags` down into the binding, leaving
only member names and engineering units at property level -- was considered and
rejected for now. Its justification was a second transport: KNX DPT 9.001 is a
16-bit float that cannot be expressed as "int16 × 0.1", and raw enum values
differ per transport. The secondary argument, that it would help the firmware
X-macro, does not hold up: whether `scale` sits inside `binding` or beside it is
one dictionary lookup either way for a generator. So it would be 209 signals of
churn for nothing. **If a second transport is ever committed, this is the first
thing to do, and it must be preceded by writing out the real bindings for three
or four properties by hand** -- D5's lesson was that `repeat` looked sound until
its first realistic example demanded an escape hatch.

**Naming.** The collection is still called `signals:`, not `properties:`, to
avoid renaming 211 entries plus every `signal:` reference in procedures and the
validator for a debatable gain. "Signal" is standard in this domain and covers
settings, measurements and commands. Worth revisiting only if it starts
misleading people.

**Open.** What the real internal property list is — only two illustrative
examples are in the file, marked as such, and the actual list must come from the
firmware author. Also whether a setting may be volatile (session-only), which
`kind: setting` currently forbids by implication.

## D9 — Semantics and protocol facts stay in one file per device

**Decided.** A device's properties and their Modbus bindings live together, one
file per device. Nothing changes in the repository; this records why, and the
measurements behind it, because the question returns whenever a second device
appears.

**Why not split a property from its binding.** The strongest argument is a
regression the split would create, and it exists only because of D8: with
bindings in a separate file keyed by name, a misspelled name produces *two*
silent bugs. The property finds no binding and therefore looks internal — which
D8 made legal — while the binding becomes an orphan. Inline, the same typo is a
single YAML structure error in one place. Splitting would cost error detection
exactly where off-bus properties made the format more permissive.

On consumers the picture is genuinely mixed, not one-sided: the settings-storage
and live-data generators need only the semantic half, while documentation, the
commissioning tool and any register-map output need both. But ignoring a field
is free and joining two files is not, so co-location wins on cost asymmetry
rather than on necessity.

**Protocol facts that are not per-property are already separate** — the
`access:` section holds `supported_fc`, the word limits, `gaps_readable` and
`turnaround_ms`. That is the right home for them: a section, not a file.

**A registers-only view is a derived output**, on the same principle as D1's
reverse index. Derive views; do not store them.

**If the file gets unwieldy, split by subsystem, never by layer.** At 2100 lines
it is already awkward. Splitting into `properties.yaml` plus `modbus.yaml` is
the worst available cut: it puts a property's meaning and its address in
different files, which is precisely the pair that gets read together. Splitting
into `fans.yaml`, `sensors.yaml`, `bypass.yaml` keeps each property whole and
helps navigation far more.

**Where separation does pay: definitions shared across devices.** Measured over
the 25 profiles in `3SModbus/DeviceLibrary`, 1169 registers total:

- 48% of all registers use a name shared by 3 or more devices — a large reuse
  surface
- but only 68% of those shared names carry identical semantics everywhere

The distribution is cleanly bimodal, which is what makes the answer tractable:

| definition | devices | distinct addresses | semantic variants |
|---|---|---|---|
| Slave Address, Baud Rate, Parity, Device Type, HW/FW Version | 20 | 1 | 1 |
| Termination Resistor, Registers Reset | 18 | 1 | 1 |
| Device Reset | 17 | 1 | 2 |
| Device Status - Warnings | 12 | 1 | 3 |
| Minimum / Maximum Output Value | 3 | — | 5 |

So roughly ten registers — the device-common block — are provably identical
across 17 to 20 devices in name, address and semantics. Those are true shared
definitions and worth extracting into a shared include.

Everything else that looks shared is shared in **name and shape only**.
`Device Status - Warnings` sits at one address in 12 devices with three
different sets of bit meanings, because each device has different warnings.
`Minimum Output Value` has five semantic variants across three devices. Pulling
those into shared definitions would need per-device overrides, and D5 settled
what an abstraction demanding an escape hatch on first contact is worth.

**Deferred, not rejected.** The common-block include is real but premature: this
repository has one device, so an include buys indirection and no reuse.
Revisit when a second device lands here, and extract only the rows in the top
two bands of that table.

## D10 — Three type facts, not one: semantic type, wire encoding, storage width

**The defect.** `binding.type: uint16` was doing three jobs at once and only
honestly doing one. Measured across the 209 bound properties, the wire type said
`uint16` (164) or `int16` (45) and nothing else. What those values actually were:
92 integer quantities, 51 enumerations, 49 real quantities, 19 bit sets. The
format never stated any of it. Firmware wanting `bool` and `typedef enum` had to
infer the type from whether an `enum:` map happened to be present, and "this is
a real" from a `scale:` being there. That worked by accident.

D8 made it worse: it left `type` inside `binding` and then made `binding`
optional. So the two internal properties had **no type information at all** —
and the settings-storage generator, the exact thing D8 was for, had nothing to
generate from.

**The rule.** Three independent facts, each where it belongs:

| fact | question | lives on | in the file |
|---|---|---|---|
| semantic type | what *is* this value? | the property | `type:` |
| wire encoding | how is it carried over Modbus? | the binding | `binding.encoding:` |
| storage width | how many bytes when persisted? | derived from range | `storage:` (override only) |

Semantic types: `bool`, `int`, `real`, `enum`, `flags`, `text`. This is the type
firmware declares — a bool as `bool`, an enum as an enum typedef — and it is
deliberately not a register width.

They are independent because the device makes them independent: one `bool` can
be a coil, a discrete input, or a bit in a register; one `real` can be `float32`
across two registers or a scaled `int16` in one, at lower resolution; a 32-bit
`int` needs two registers whatever it means. `binding.type` was renamed to
`binding.encoding` so it stops looking like the value's type, and the register
span now follows from the encoding width rather than being stated — `count` is
array length only.

**Storage is derived, not declared.** Following D1: the width comes from `range`
(divided by `scale` for a real), falling back to the binding encoding, and
`storage:` is an override for when the device stores wider than the range needs.
A declared width is checked against the range either way, and a property where
none of the three is available is an error — which is exactly the hole D8 left.

**Because the three are now separate, seven classes of error became detectable.**
All verified to fire:

- an enum or a bool carried as `float32`
- a real on an integer encoding with no scale, so its fraction is unrepresentable
- a range that does not survive its encoding (`[0, 4294967295]` in a `uint16`)
- a flag bit outside the encoding's width (bit 20 in a `uint16`)
- an enum value too large for its encoding
- a declared storage width too small for the range
- a storage width that cannot be derived at all

The compatibility table (which encodings can carry which type) lives in
`validate.py` rather than the schema, because it is a many-to-many relation and
reads as a table there instead of as nested `if`/`then`.

**Schema-enforced consistency:** `type: enum` requires an `enum` map and nothing
else may carry one; same for `flags`; `scale` and `decimals` are rejected on
`enum`, `flags`, `bool` and `text`; `max_length` belongs to `text` and only
`text`. Procedure parameters now take semantic types too — they were still
declaring `uint16`, which the new schema caught.

**Not changed:** the 24 two-member enums (`disabled`/`enabled`,
`disconnected`/`connected`) stay enums rather than becoming bools. The library
models them as two-entry dictionaries, the names carry domain meaning a boolean
would throw away, and an enum can grow a third member while a bool cannot.
`bool` exists for values that are genuinely truth values, and matters most for
coil and discrete bindings — which this device does not currently use.

**Open.** `text` is defined but unused; the hardware and firmware version
registers are still `int` when they are really packed hex bytes (noted in D6),
and `text` with a `chars` encoding may or may not be the right home for them.
Whether a real should ever be stored as `float32` on-device while travelling as
a scaled integer is untested — the format allows it, nothing exercises it.

## D11 — Booleans are booleans; a bit field is not a type

Three corrections, all the same mistake: the type system described the Modbus
representation instead of the device.

### Two-member enums become bool

D10 kept 24 two-member enums as enums, arguing an enum can grow a third member.
That was wrong. `disabled`/`enabled` is a boolean whose states have names; the
names are for display, not evidence of a third state. Firmware wants `bool`.

`type: bool` now takes an optional `labels: { false: disabled, true: enabled }`,
which is what documentation and a commissioning tool show and what an expression
compares against, so `client_enable == enabled` still reads the same while the
generated code gets a `bool`.

The test is whether one member is the *absence or negation* of the other.
Twenty pairs are: enabled/disabled, installed/not, connected/disconnected,
low/high, normal/swapped, automatic/overwrite, normally open/closed, idle/reset,
idle/rescan. Two pairs are not, and stay enums, because they are peer
alternatives that could plausibly gain a third:
`analog_output` / `modbus` for fan control source, and `softstart` / `kickstart`.

### A status register is N bool properties, not one flags value

In the existing products these registers are internally one boolean property per
flag, and the Modbus register is a packed *view* of them. The profile now says
that: each bit is its own bool property with
`binding: { input: 1, encoding: bit, bit: 0 }`, and the register is recovered by
grouping bools that share an address — derived, per D1, not declared.

`type: flags`, the `flag_map` definition, the `contains` / `not contains`
expression operator and the `contains` / `not_contains` condition forms are all
**removed**. 19 flags registers became 87 bool properties, and 114 expressions
using `contains` became plain boolean references:

    before:  present_when: supply_ch1_sensor_capability contains temperature
             valid_when:   ... and supply_ch1_sensor_state not contains sensor_problem
    after:   present_when: supply_ch1_provides_temperature
             valid_when:   ... and not supply_ch1_sensor_problem

**This shrinks the format.** The most load-bearing part of the file lost an
operator, which matters for D3: the grammar no longer needs set membership, only
comparison, boolean connectives, and a bare boolean reference. A bit is now an
ordinary property with its own title, and each one is individually documentable.

The packed register's human name has nowhere to live once the register is not a
property, so `bitfield: "Device Status - Errors"` carries it on each bit.
Grouping is by address; the field only supplies the name, and every bit at one
address must give the same one — checked.

**Four new checks, all verified to fire.** They matter more now precisely
because an address is shared by up to sixteen properties, making a mistyped
address or a repeated bit a plausible hand-editing slip under D5:

- two bools claiming the same bit of the same register
- two properties claiming the same whole register (this check did not exist
  before, and catches ordinary copy-paste address errors anywhere in the file)
- bits at one address disagreeing about the packed register's name
- a register used both as a whole value and as packed bits

### Strings versus formatted integers

The library settles this: of its 55 `bytewise` decoders, 50 are
`hexadecimal` with separator `.` and leading zero trimmed — that is a *version
display* of `0x0130` as "1.30", not text. Only 5 are `unicode`, on
"Source code version - Temporary", a genuine string.

So the two cases are different and are modelled differently:

- a version is `type: int` with `display: version`. The value is an integer; only
  its rendering is special. `display` also allows `decimal` and `hex`. Applied to
  `hardware_version` and `firmware_version`.
- a real string is `type: text` with `encoding: chars`, `count` registers and
  `max_length` characters, two characters per register.

**Scale.** 279 properties (up from 211), 3009 lines: 107 bool, 92 int, 49 real,
31 enum. 87 of the bools are bit-bound across 19 packed registers.

**Open.** `text` is still unexercised — this device exposes no string, so the
character order within a register and whether trimming belongs in the profile at
all are undecided. `display: version` describes the library's format but the
profile does not say which byte is major and which minor.

## D12 — A binding names its address space explicitly

**Decided.** A binding is `{ space, address, encoding }`, all three required:

    binding: { space: holding, address: 41, encoding: uint16 }
    binding: { space: input, address: 1, encoding: bit, bit: 0 }

Previously the address space was carried by *which key* held the address —
`{ holding: 41 }` versus `{ input: 41 }`. That worked but made the space
implicit in the document's shape rather than stated, with three costs:

- the schema needed a four-branch `oneOf` over mutually exclusive keys, and
  every per-space rule had to be written four times
- every consumer had to probe four keys to discover the space before it could
  do anything, including `validate.py`, which did exactly that
- nothing forced an encoding to be present, because there was no single
  required shape to hang it on

With `space` as a value, the binding is one regular record. All 277 bindings
were rewritten mechanically; the spaces in use are 130 holding and 147 input.

**It also made a rule sayable that was previously awkward.** A coil and a
discrete input *are* single bits, so the address already identifies the bit:
in those spaces the encoding is `bit` and a `bit:` index is forbidden. In
holding or input space, `encoding: bit` means a bool packed inside a register
and the `bit:` index is required. `coil` and `discrete` are therefore no longer
encodings — they were never packing formats, only places.

**Address spaces are independent ranges.** The same number in two spaces is two
different values, which the profile already relies on: `holding 1` is the slave
address while `input 1` holds the error bits. D11's collision checks key on
(space, address), so they catch a real clash inside one space without
complaining about that.

## D13 — Where documentation prose lives

Asked while planning a generated Modbus register map: where does a register
description go?

**Per property: `description`, which already existed and was unused.** The
schema has carried `description` on every property, on each procedure and on
`device` since D2. Zero of 279 properties used it. Nothing needed inventing;
the field needs filling, and only the device author can fill most of it.

**Prose belongs in fields, not comments.** The profile held 120 comment lines,
including the reason air path temperatures name a source channel, the
PROVISIONAL note on filter monitoring, and every section heading. A generator
cannot read any of it, so prose a reader of the documentation needs is not
allowed to live in a comment. Five property-level comments were converted to
`description` as the pattern to follow: left_right_swap, modbus_safety_timeout,
heat_recovery_efficiency, preheater_installed, bypass_minimum_output_value.
Comments remain fine for notes aimed at whoever edits the profile -- the D7
PROVISIONAL note is genuinely of that kind.

**Packed registers get a `bitfields:` section, which also removes a
duplication.** D11 made each bit its own property and left the packed register
with no home for prose, and worse, repeated its title string on all 87 bits with
a check that they agreed. Now each packed register is declared once, with a
title and a description, and bits reference it by id:

    bitfields:
      device_status_errors:
        title: "Device Status - Errors"
        description: >-
          Fault conditions that stop normal operation. ...

    error_memory_fault:
      bitfield: device_status_errors

19 declarations replace 87 repeated strings, and the class of error the old
check looked for -- bits disagreeing about their register's name -- is now
unrepresentable rather than merely detected.

The declaration deliberately carries **no address**: it is derived from the bits
that reference it, per D1, and the validator reports a packed register whose bits
are spread over more than one register, a reference to an undeclared bitfield,
and a declared bitfield nobody references.

This is not a return to the `registers:` section deleted in D6. That one was a
substitute for properties -- raw registers that should have been signals.
A packed register genuinely is not a property: its bits are.

**Two gaps left open, both needing a call on verbosity:**

- *Enum member prose.* 125 enum members have nowhere to carry an explanation,
  because `enum` maps a value straight to a name. Options: a sparse parallel
  `enum_descriptions` keyed by member name, paid only where prose exists; or
  turning `enum` into a list of `{value, name, description}` records, which is
  uniform and matches the library's `entries` shape but adds roughly 180 lines
  for the members that need no prose.
- *Section headings.* The 15 section headings are comments, so a generated map
  is one flat 279-row table. A `sections:` section referenced by a `section:`
  id per property would mirror how `bitfields` now works, robust to addresses
  moving, at the cost of one short line per property. Declaring sections as
  address ranges instead avoids that line but drifts whenever a register moves.

## D14 — D9 re-examined against version churn, and still stands

Revisited after D10-D13 moved a lot of ground: is one file for both semantics
and protocol still right?

**The layer boundary is sharper than it was, so the question is fair.** D10 gave
the property a semantic `type` and `storage` and pushed encoding into the
binding; D12 made the binding a regular `{space, address, encoding}` record;
D13 added `bitfields:`, which is purely a protocol and documentation entity.
Three of the five top-level sections -- `access`, `bitfields`, `procedures` --
are now entirely protocol.

**But the measured split did not move: 82%, against 83% at D9.** The only
protocol content at property level is `binding` (277), `bitfield` (87) and
`on_write` (23), against 1746 semantic key occurrences.

**The decisive new evidence is version churn.** D9 measured sharing across
devices; the open question was sharing across firmware versions of one device,
where a stable semantic model with moving addresses would favour splitting.
Measured over the library's four version steps (DSCDG0-4 1.40 -> 1.50 -> 1.60,
TSVCT 1.0 -> 1.20 -> 1.30):

| change between consecutive versions | count |
|---|---|
| register unchanged | 166 |
| address changed, semantics identical | **2** |
| semantics changed, address identical | 6 |
| both changed | 1 |
| register added | 34 |
| register removed | 76 |

Address-only churn is 2 events out of 112 changes across four revisions. What
actually churns is whole registers appearing and disappearing -- 110 events --
and every one of those touches both layers at once. Splitting by layer would
optimise the case that essentially never happens while making the common case
require coordinated edits in two files that can drift apart.

So D9 holds, now on evidence rather than on argument.

**One of D9's arguments was wrong, and fixing it removed a live hazard.** D9's
strongest claim was that a split would let a mistyped name silently turn a
register into an internal property, since D8 made a missing binding mean
internal. That hazard was never about splitting: it exists today in the single
file. Delete a `binding:` line by accident and the register disappears from the
map with no complaint, because absence was carrying meaning.

Internal is now stated, not inferred. A property must have either a `binding`
or `internal: true`, never both, and `internal: false` may not be written. A
lost binding line is now an error instead of a silent deletion from the register
map. This is worth having regardless of the split question, and it means the
decision above rests on the churn data alone.

**Unchanged advice on size.** At 3009 lines the file is unwieldy, and the answer
is still D9's: split by subsystem, never by layer, because a layer split
separates the one pair -- a property's meaning and its address -- that is always
read together.

## D15 — Registers are entities. Two documents, and D9/D14 are overturned

**The complaint that broke it.** `bitfields:` should not exist, and a register
carrying several properties has no clean place for its description. Both are
symptoms of one defect: **the property-to-register relation is many-to-many, and
it was modelled as a per-property attribute.**

Measured on the profile as it stood:

- 87 properties shared 19 registers -- N properties to 1 register, which a
  per-property `binding` simply cannot express. `bitfields:` was the side-table
  that hid it, and it broke both D1 (you could not read a property and know its
  register without following a reference) and D5 (the register's identity lived
  apart from its bits)
- 1 property to N registers is implied too, by `uint32`, `float32` and `chars`
- 23 unoccupied holding ranges were undescribable, because a reserved or
  withdrawn range belongs to no property at all
- register title, description and access had no owner. For a packed status word,
  read-only is a fact about the register, not about each of its eight bits

**The fix: a register is an entity.** It owns its space, address, title, prose
and access, and its `fields` say which properties it carries and how:

    - space: input
      address: 1
      title: "Device Status - Errors"
      description: >-
        Fault conditions that stop normal operation.
      access: read_only
      fields:
        - { bit: 0, property: error_supply_voltage_fault }
        - { bit: 1, property: error_internal_voltage_fault }

    - space: holding
      from: 7
      to: 8
      reserved: "Reserved for future interface settings."

`bitfields:` is gone. N:1 is a field list, 1:N is a field with a wide encoding,
a reserved range is a register carrying nothing, and a register with exactly one
field needs no title -- it borrows its property's, so nothing is duplicated.

**Two documents.** `device-properties.yaml` holds the semantic layer and
`device-modbus.yaml` the register map, each with its own schema. Once registers
own their identity the protocol layer is self-contained and references properties
by name, so a second interface could be added later as another document without
touching the first. `signals:` became `properties:` and `signal:` became
`property:`, since the word now covers settings, live data and commands.

**This overturns D9 and D14, and the reason is worth recording.** Both defended
one file on *locality*: a property's meaning and its address are read together.
That argument only holds while the address is a property attribute -- so it was
defending the defect, not the design. Once registers are entities, locality is
already gone and the file count is a minor consequence.

D14 also overstated the cost of coordinated edits. The two documents hold
**complementary** facts, not duplicated ones: adding a register genuinely is two
facts -- what it means, and where it lives. Nothing can disagree, because nothing
is stated twice.

D14's churn measurement stands as data and is simply not an argument against
this: it measured edit coupling, not drift.

**Safety is recovered as a check, not lost.** What a single file gave for free
was completeness -- a property without an address was visibly incomplete.
`validate.py` now has a third layer for exactly that, and every check below was
verified to fire:

- a property carried by no register and not marked internal (the case that used
  to be a silently deleted `binding:` line)
- a field naming a property that does not exist
- a property carried by two registers
- an internal property that a register carries anyway
- a type its encoding cannot carry, and a real on an integer encoding with no scale
- a non-bool given a bit position
- a wide encoding overlapping the following register
- a reserved range over an occupied register
- a register whose declared access disagrees with its properties

**Scale.** 279 properties in the semantic document, 209 registers in the map,
277 fields. Two schemas, 61 negative tests.

**Open.** The register map still describes none of its 23 unoccupied ranges:
whether each is reserved, withdrawn or merely unassigned is a firmware fact. The
`sections:` question from D13 is now easier -- a section is a run of registers in
the map, so it belongs to the protocol document.

## D16 — Correction to D15's wording; the register map carries only what cannot be derived

**D15's wording was wrong, and the model it describes is right.** D15 said the
property-to-register relation is many-to-many. No single mapping ever is: a
property occupies one register or N consecutive ones, and a register carries one
property or N bits of them, and the two never interleave. Each mapping is a tree,
not a tangle. What is many-to-many is only the relation across the two *sets*.

This matters because it bounds what `fields` has to express -- N:1 for bits and
1:N for a wide encoding at one start address -- and that is exactly what it does.
The defect D15 fixed was real; the sentence describing it was loose.

**Register access was duplication, and I had written a checker for it.** Measured:
the register-level `access` was identical to the access of the property it carried
in **209 of 209** cases, and `validate.py` contained a check that the two *agreed*.
That is precisely D1's anti-pattern -- two copies of one fact with a checker
standing in for a single source -- committed inside the format built to avoid it.

`access` is now derived from the properties a register carries. It may still be
given, but only to **narrow**: a setting the device lets a local interface change
while the bus exposes it read-only. Widening is an error. 209 lines removed; the
register map went from 1246 lines to 1044.

**Encoding stays explicit, and the measurement is why.** The obvious Modbus width
is correct in 178 of 190 cases, which looks like an invitation to derive it. It is
a trap: if encoding followed from `range`, then widening `[5.0, 25.0]` to
`[-5.0, 25.0]` in the semantic document would silently flip the wire format from
`uint16` to `int16` -- a protocol break with no diff in the protocol document. The
12 exceptions are that case made visible: temperature setpoints declared `int16`
though their present range is positive. Recorded in the schema so nobody
"simplifies" it later.

**"It looks like the JSON files I already have."** It does, because it describes
the same thing -- and that is worth having rather than arguing about, so
`generate_library_json.py` produces a 3SModbus DeviceLibrary profile from the two
documents. It **validates clean against the library's own schemas, 0 errors**,
reproducing their conventions exactly: `HR11 contains enabled` conditions,
bitwise decoders with per-bit labels, two-entry dictionaries for booleans, and
bytewise hex for versions.

So the relationship is superset, not rivalry. The library format repeats the
semantics into every register -- units, decoder ranges, defaults -- while these
documents hold them once and reference them. Adoption does not require rewriting
3SModbus or its 25 profiles: they can be generated.

This is also the format's **first actual consumer**, and it earned its keep
immediately by finding a missing field. The library's bitwise decoder requires a
label for "no bit set", and nothing here carried one, so the generator had to
invent "OK" nineteen times. `clear_label` now sits on each packed register --
"OK", or "No measurands reported" for a capability word. A register map document
needs it for exactly the same reason.

**What still does not survive the conversion: 3.** Two internal properties, which
have no register by definition, and one condition: heat recovery efficiency
depends on three temperature sources being assigned, and the library's condition
grammar is a single comparison. That is the honest measure of what the richer
model buys -- not much for this device, and precisely the conjunction that D3
will have to define.

The generated file is a derived artifact and is gitignored, per D1.

## D17 — The protocol document stands on its own

*Settled form of what was first recorded as D17, D19 and D22; the git history
holds the intermediate spellings and the arguments that moved them.*

**The requirement.** `device-modbus.yaml` has to describe the access API of any
Modbus device, a third party's as readily as ours. That means everything a client
needs in order to talk to the device is in this file, and nothing is inferred
from a semantic document that a third-party device does not have, or from
firmware source nobody outside the vendor can read.

Four things stood between it and that. Each is stated below as the rule, then the
reason the obvious cheaper answer does not work.

### Rule 1 — the document says which device it describes

    device:
      vendor: "3S"
      model: "AHU-1"
      type: 4010
      firmware_version:
        from: "1.0"

`vendor` and `model` are required. `vendor` explicitly, because the format
describes other people's products, where "the device" is not obvious from the
repository it sits in. The firmware bound is a **range**, not a version, since a
register map normally outlives several releases; omitting `to` means "and later".

Before this the document's top-level keys were exactly `['limits', 'registers']`:
an anonymous list of addresses. D14 measured edit churn across firmware versions
of one device and D15 split the documents partly so a second interface could be
added later — both make this binding load-bearing, and it was absent from both
documents.

### Rule 2 — each address space declares what its numbering counts from

    spaces:
      holding:
        base: 1
        registers: [ ... ]

`base` is the address this document would write for **wire offset 0** in that
space, so every address in the file is read as `offset = address - base`. Write 1
for datasheet numbering, 0 to write raw offsets, or the legacy Modicon base
directly — `40001` for holding, `30001` for input — which lets a map be
transcribed exactly as printed. It sits on the space because it is a fact about
that space's numbering and about nothing else (D20).

**Why this is required rather than defaulted.** The file said `address: 1`, the
schema permitted `address: 0`, and `generate_library_json.py` copied the address
straight into the library's 1-based register number. Three conventions, none
stated, and a probe confirmed a register at address 0 passed the schema and every
validator layer. This is the one off-by-one that lands in the documentation, the
commissioning tool and the firmware simultaneously, with no diff anywhere to show
it, and it is what a transcribed third-party map is most likely to get wrong.
Nothing about it is derivable. A default would be the unstated convention again,
spelled differently.

**Rejected: a named convention.** `base: pdu | data_model`, with a
`base_overrides` map for a device numbering one space differently, was the first
form. Two mechanisms and a private vocabulary to express one integer, and the
vocabulary had to be learned before the file could be read. The number is also
strictly more capable: the enum could say 0 or 1 and nothing else, so legacy
numbering was expressible only by instructing the author to strip the prefix —
after which the map silently differed from the datasheet it was copied from.

**Also rejected: naming the spaces `holding_registers` / `input_registers`.** A
consumer resolves the space by the same word `space:` already uses, with no
mapping table in between — and a coil is not a register, so the longer spelling
would be wrong for half the spaces.

**It also fixed a live bug.** The generator emitted `"number": reg["address"]`,
which is correct only when the base happens to be 1. It now computes
`address - base + 1`. The output is unchanged for this device, which is precisely
why the bug went unnoticed: one profile with base 1 cannot distinguish the two.

### Rule 3 — a coil is a single bit

A coil or discrete input carries exactly one property, has no encoding to choose,
no `bit:` position to give — the address already was one — and no `clear_label`,
since there is no set of bits to be collectively clear. Conversely `bit` is not an
encoding a 16-bit register can have, because it would not say which bit. `count`
is confined to `chars`, being an element count on an encoding that has no
elements otherwise.

Since D20 these are not rules the schema checks but the shape it describes: a bit
space and a word space hold different kinds of entry, so the nonsense below is
unrepresentable rather than rejected.

**Why it needed rules at all.** All three of these were accepted by the schema
*and* by the validator: a coil carrying `uint16`, a coil field with `bit: 3`, a
discrete input carrying `float32`. Both spaces were declared vocabulary with zero
uses and zero checks, so the format promised bit-oriented spaces while accepting
physically impossible maps in them. Zero uses is one too few to leave unchecked:
the AHU has no coils, and the first third-party device with one would have
written nonsense that validated.

### Rule 4 — the document says how to recognise the device

    identification:
      read_device_id:
        vendor_name: "3S"
        product_code: "AHU-1"
        conformity_level: 1
      registers:
        - { space: holding, address: 4, equals: 4010 }
        - { space: holding, address: 6, mask: 0xFF00, equals: 0x0100 }

A bus scan finds an address, not a product. Without this, choosing which profile
applies is left to whoever is holding the laptop.

**Read Device Identification (FC 43, MEI type 14) comes first where it answers.**
It is the only way to ask a Modbus device what it is without already knowing its
register map. A register probe reads an address chosen from *this* profile, which
on another vendor's product may be reserved, absent, or meaningful in a way that
coincidentally matches; FC 43 cannot make that mistake, because the question is
about identity rather than about an address. `conformity_level` is stated because
it decides which objects a client may ask for at all — a level 1 device returns
only vendor name, product code and revision — and whether individual objects may
be requested by id rather than streamed. Getting that wrong turns a safe
identification call into an exception.

**It is not the only mechanism, because FC 43 is optional in Modbus and widely
unimplemented.** The second real device in front of this format, the DSCDG3-4
duct sensor, identifies itself through a device-type holding register and nothing
else. A format requiring FC 43 would describe a minority of the devices it is
meant to cover. Hence both, with at least one required and either accepted alone.
`limits.supported_fc` accordingly admits 17 and 43 alongside the data-access
codes, so a device can declare the call a profile relies on; expecting FC 43
objects from a device that does not answer FC 43 is a profile that could never
identify anything, and `validate.py` rejects that pair.

**Four properties the probe shape was chosen for:**

- **Read-only.** Probing an unidentified device must never change it. There is no
  write probe and no unlock step.
- **A failed read is a mismatch, not an error.** The thing being probed may be
  another vendor's product, where the address is reserved, absent, or answers
  exception 02. Treating that as a fault would make detection unusable on exactly
  the bus it exists for.
- **Raw values, never decoded ones.** A scale or an enum mapping presumes the
  profile being tested, so comparing decoded values would beg the question.
  `mask` covers a register packing a product id beside something volatile.
- **Conjunction, cheapest rule first.** Every rule must hold, so a profile
  matching nothing is safer than one matching the wrong device, and a client may
  stop at the first mismatch.

### What lives in the validator rather than the schema

Everything that depends on a sibling section, since a JSON Schema cannot see one:

- a space the map uses has to declare a base, and a base declared for a space
  nothing uses is a warning
- an address has to fall inside `base .. base + 0xFFFF`; the schema keeps only a
  bound generous enough for six-digit numbering
- no register probed twice — two rules on one register either agree, and one is
  noise, or disagree, and nothing matches
- no expected value with bits set outside its own mask, and no empty range
- no probe on a coil or discrete input: one bit cannot discriminate between
  products
- FC 43 expectations require 43 in `supported_fc`, and a device declaring 43 with
  nothing to match against is an identification path left on the floor

A probe naming an address outside the documented map is a warning rather than an
error — a device id register may legitimately sit outside it, but the usual cause
is a typo. A document with no `identification` at all is a warning too.

### Cost

Three formerly optional sections are required, so every existing map needs a
header before it validates — acceptable at two documents, and the reason to do it
at two rather than at twenty. `identification` is also the first vocabulary here
aimed at a consumer's *behaviour* rather than at the device's facts, which sits
slightly uneasily beside D16's rule that the map carries only what cannot be
derived. It earns its place because nothing else in either document answers "is
this that device?", and that has to be answered before any other fact in the file
can be trusted.

### Left open

Whether two profiles' identification rules are mutually exclusive is checkable
and cannot be checked from inside one document — it needs a library of them. That
is the right check to add when there is a directory of profiles rather than one.

## D18 — Every register carries its own title and description, because nothing else can

**Asked directly: is it written down anywhere that a register needs a
description in order to generate documentation?** It was not. This entry is
that rule.

What existed instead was an accident. D13 settled where prose lives and gave
the right reason -- a generator cannot read a YAML comment, so prose a reader
of the documentation needs may not live in one -- but it answered for
*properties*, and for packed registers through the `bitfields:` mechanism that
D15 then deleted. D15 listed "register title, description and access had no
owner" as a symptom motivating registers-as-entities, which is a diagnosis of
the old design rather than a rule for the new one. D16 revisited `access` and
removed it as derivable, and never came back to prose. D17 made the document
standalone and did not mention it either.

The schema recorded the accident faithfully: `title` was required only on a
register carrying two or more properties, on the reasoning that a lone field
lends its own; `description` was required nowhere at all.

**Measured, which shows it was convention and not design:** of 209 registers,
19 carry a description and 19 carry a title -- exactly the 19 packed registers
the schema forced a title onto. All 190 single-field registers carry neither.
87 bit fields carry no title either.

**Rule.** A register that carries anything declares a `title` and a
`description`. Where it carries more than one field, every field declares a
`title` as well. A reserved range is exempt: `reserved` already says why it
carries nothing, and a range has no name to give.

**Why it cannot be derived, borrowed or defaulted.** The previous answer was
that a single-field register borrows both from the property it carries. That
answer died with D17: this document has to describe a third-party device, where
there is no semantic document to borrow from and no firmware source to read.
The register map is then the *sole* input to generated documentation, and a
register with no prose produces a table row with an address, an encoding, and a
blank column where the explanation goes. Nothing derives prose. Nothing else
can hold it.

This is not the duplication D1 and D16 warn about. Duplication is the same fact
in two places with nothing keeping them equal. A register's name and purpose in
the protocol document is not a copy of anything -- when a semantic layer also
exists it holds a different fact, what the value *means*, which is why the two
documents were split in the first place (D15). Where the strings do coincide,
that coincidence belongs to the generator that emits one from the other, not to
the format.

**Cost, and it is the largest of any rule here.** 190 registers and 87 fields
need prose written before this map is documentation-complete. That prose is a
device fact, so only the firmware author can supply most of it, and inventing
190 restatements of the identifier -- "Supply Channel 1 Carbon Dioxide Alert 1
Level" described as "supply channel 1 carbon dioxide alert 1 level" -- would be
worse than the gap, because it would satisfy the check while telling a reader
nothing and hiding the 190 real omissions behind 190 apparent successes.

**So the rule is a validator warning, not a schema requirement, and that is
deliberate.** Putting it in the schema would make `device-modbus.yaml` fail its
own schema, which in turn breaks the baseline assertion in `test_schema.py` --
the check that the schema describes the real document rather than an aspiration.
A rule whose only effect is to make the flagship document invalid is not
enforcement, it is a broken build. `validate.py` instead reports the three
counts on every run, so the gap is visible and quantified rather than implicit.
It becomes a schema requirement when the count reaches zero, and that step is
mechanical once the prose exists.

**What this closes.** Nothing in section A or B of PROTOCOL-GAPS.md -- this is a
gap the audit missed, because the audit asked what a *client* needs to talk to
the device and this is what a *reader* needs to understand it.

## D19 — Exception codes are part of the interface

*Settled form of what was first recorded as D20 and D21; the git history holds
the intermediate spellings and the arguments that moved them.*

**Rule.** A top-level `exceptions:` list says which codes the device raises and
what it means by each. Every entry carries `code`, a short `name` and a
`description`; `retryable` and `overloads` are optional.

    exceptions:
      - code: 0x03
        name: "Illegal Data Value"
        description: >-
          A written value outside the range declared for the register. The device
          rejects the whole request rather than clamping, so a partially valid
          multi-register write applies nothing.
      - code: 0x41
        name: "Setting Locked In Current Mode"
        description: >-
          Writable in principle, but not in the mode the unit is in. Change the
          governing setting first; retrying unchanged will fail identically.
        retryable: false

**Why the specification is not enough, in two distinct ways.** A manufacturer may
*overload* a standard code, so a client falling back on its own table displays
"Illegal Data Value" for a code this device uses to mean something narrower —
confidently wrong, which is worse than blank. And a manufacturer may define codes
of its own, which a client can otherwise only render as a bare number: the
integrator sees `exception 0x41` and has nothing to do with it.

Underneath both is what an exception table is really for. Without it an
integrator cannot distinguish a *refused* write from a *broken bus* — the
difference between a configuration mistake and a site visit.

**Both texts are required on every entry, standard codes included.** `name` is
the short label for inline display — a status line, a log entry, a cell in a
table of failures — where there is no room for the description. It is **capped at
40 characters**, and the cap is not arbitrary: the longest name Modbus itself
uses is "Gateway Target Device Failed To Respond" at 39. A consumer can therefore
lay out a fixed column and know nothing will overflow it, and never needs a table
of its own in order to render a failure.

It is deliberately not called `title`, which is what a register's short text is
called. A register title is a full uncapped name — "Supply Channel 1 - Carbon
Dioxide Alert 1 Level" is 46 characters — and would not fit the use this field
exists for. Two names for two different constraints is the lesser cost against
one name that silently means "capped" in one place and "uncapped" in another.

**`overloads` is the machine-readable warning, not a way of supplying text.**
Since name and description are always given, its job is to tell a client that
knows the standard table to stop applying logic keyed to the standard meaning —
retry policy, diagnosis, how it phrases the failure to an installer — and to let
generated documentation mark the code as used non-standardly. It is meaningful
only on a standard code: a proprietary code overloads nothing, having had no
prior meaning to depart from, so it is an error there rather than a redundancy.
It is never written as `false`; an absent key already says that.

**What is derived rather than declared**, per D16. Whether a code is standard at
all follows from the number, so nothing declares it; `validate.py` holds the
table, including that 7 and 9 were never assigned and a device using either is
therefore proprietary. Retryability is likewise implied for standard codes — 5
and 6 are retryable, 1 to 3 are not — so `retryable` exists for the proprietary
codes, where a client has no way to guess, and stating it on a standard code in
contradiction of the implication is an error rather than an override.

**Checks that need the standard table or a sibling section:** no code declared
twice; `overloads` only on a standard code; no `retryable` contradicting a
standard meaning; a code marked as overloading while still carrying the standard
name, where nothing a user sees would show that it differs; and
`limits.gaps_readable: false` — a claim that reading an unoccupied address is
refused, and therefore a claim about code 0x02, which then has to be in the list.

**Rejected: `raised_by`.** A list of the function codes that can return each
exception. It was justified by the cross-check it enabled — every code named had
to appear in `limits.supported_fc` — which is circular: the field was invented,
and then the check on the invented field was offered as the field's value. A
check is worth only what the data it checks is worth.

Measured against a consumer, the data is worth nothing. A client handling an
exception already knows which function code it sent; what it needs is a lookup on
the code it *received*. Nothing asks "which functions can return code 3" — not
the documentation generator, not a commissioning tool, not firmware. It was also
unanswerable in practice, since enumerating it is firmware knowledge nobody has
to hand, so all four of the AHU's entries were guesses. A field both unused and
probably false is worse than an absent one: it lends false authority.

**Also rejected: "silence means the standard meaning".** Letting a standard code
be listed bare, with the client supplying its text, kept the section terse and
was the first form. It fails the thing the section is for: a consuming tool
displaying a failure inline would need its own table, and would not have one for
proprietary codes. The verbosity it protected against is not real — a device
raises a handful of codes, and all six of the AHU's entries carried descriptions
already. Dropping it also removed both of the schema's conditional rules, so
`$defs/exception` has no `allOf` at all.

**This closes gap B5**, and gives `limits` a reader beyond D17's FC 43 check, so
PROTOCOL-GAPS.md's charge that the block is decorative no longer holds — though
`max_read_words`, `turnaround_ms` and the rest are still consumed by nothing.

**Cost, and an honest note on the AHU's entries.** Six exceptions are listed and
every one is an assumption, including the claim that an out-of-range write is
rejected rather than clamped — which is gap B1, still open, and now at least
written where a firmware author can contradict it. The two proprietary codes are
invented outright: the *need* is real, since the standard has no way to say "the
register is real and writable, just not right now", and a mode-dependent or
interlocked setting has to report exactly that. The numbers 0x41 and 0x42 are
placeholders. All of it is marked in the document's ASSUMPTIONS block.

**Left open.** Which exception a *specific register* raises is not expressible.
A register-level exception list would be more precise and much more verbose, and
nothing has needed it; the two proprietary codes name their circumstances in
prose instead.

## D20 — An address space is an entity; it owns its numbering and its registers

**Rule.** `registers:` as one flat list tagged with `space:` is replaced by
`spaces:`, a container per address space:

    spaces:
      holding:
        base: 1
        registers:
          - address: 1
            fields: [ { property: server_slave_address, encoding: uint16 } ]
      input:
        base: 1
        registers:
          - ...

A register no longer carries its own space; the block it sits in is its space,
and that block also fixes the numbering its addresses are written in.

**Why, and this took two wrong answers first.** The case for grouping was
initially argued on three grounds and two of them were wrong:

- *the addressing base belongs on the space* — correct, and the surviving
  argument of the three
- *`supported_fc` secretly encodes per-space capability, so grouping makes it
  explicit* — wrong. Access is per **register**, not per space: a device's type,
  hardware version and firmware version are read-only holding registers, and
  products exist that use holding registers exclusively and mark each one's
  access individually. Which function code reaches which space is definitional
  and needs declaring nowhere.
- *a space needs somewhere to hold its own limits* — not yet true of anything
  measured, and the same speculative mistake as `base_overrides` before it

What actually decides it is the third rule of D17. "A coil carries exactly one
bit" was three `if/then` rules keyed on `space` **inside** the register object.
Once the space is the container those conditionals cannot see it — so the rule had
to move somewhere, and the honest place is the shape: a bit space holds
`bit_register` entries, a word space holds `word_register` entries, and `bit`
comes out of the word-space encoding enum entirely. Three conditionals became two
types, and the nonsense they rejected is now unrepresentable. That is the same
conversion D15 made with `bitfields:` and D19 made by requiring exception text
unconditionally: a check becomes a structure.

A second rule went the same way. "A space the map uses must declare a base" was a
validator error; a register cannot now be written outside a space block, and a
block cannot omit its base, so the check is deleted rather than kept.

**A bit register names its property directly.**

    coil:
      base: 1
      registers:
        - address: 5
          property: supply_fan_enable

Not `fields: [ { property: supply_fan_enable } ]`. A list of one exists only to be
indexed, and a coil can never hold two of anything. `validate.py` normalises it
into the field shape every other check expects, so exactly one function knows
about the difference.

**The cost, stated plainly: register identity becomes positional.** Under the flat
list, `- space: holding, address: 4, ...` was self-contained — it could be cut,
pasted, quoted in a bug report or emailed and still mean holding register 4.
Now `- address: 4` means nothing without its container, and this map holds both
`holding 1` and `input 1`, which are different registers. Paste an entry into the
wrong block and its identity changes silently.

That cuts against D15, whose formulation began "a register is an entity: **it owns
its space**, address, title, prose and access". Grouping takes that ownership
away. It is worth being honest about how much weight this carries: nothing can
*disagree* — the space is still stated exactly once, on the container — so this is
robustness and ergonomics, not correctness. It is mitigated by making every
validator path name the space, so a message reads
`spaces/input/registers/57/fields/0` rather than `registers/130/fields/0`, which
is strictly more useful than before.

**What was deliberately not moved into a space.** `gaps_readable`,
`max_read_words` and `max_write_words` are all arguably space-scoped, and all
three stay in `limits`. Nothing measured shows a product varying them per space,
and inventing the capability is exactly the mistake `base_overrides` and
space-level `access` already were. The container now exists, so moving one later
is a one-line change against evidence rather than a guess.

**Cost.** 209 registers reindented and every walk in `validate.py` and
`generate_library_json.py` rewritten to iterate spaces. Two helpers absorb the
churn: `iter_registers`, which yields `(space, register, path)`, and `fields_of`,
which hides the bit-register shape. The reindent found one latent bug on the way
— `decoder_for` read `reg["space"]` on a code path only reachable when a packed
register lacks a `clear_label`, which would now have been a `KeyError`.

## D21 — One document. A field owns its meaning, and access is stated because the protocol guarantees nothing

**Rule.** `device-properties.yaml` and its schema are gone. Everything a Modbus
consumer can use moved into the register map: a field now carries its property's
identifier, its wire encoding *and* what the value means.

    - address: 23
      title: "Ventilation Level"
      access: read_write
      fields:
        - property: ventilation_level
          encoding: uint16
          type: int
          unit: "%"
          range: [0, 100]
          step: 5
          default: 50
          writable_when: operating_mode == manual

A register keeps what belongs to the register — title, prose, access, `hidden`,
`clear_label`. A field keeps what belongs to one value. `procedures:` came across
whole, because every step in one is a Modbus operation.

**Why the split had to end.** D15 separated the documents so the protocol layer
could be self-contained, and D17 finished the job for a third-party device, where
there is no semantic document to pair with. At that point the second file was
serving only our own device, and the map was the document every consumer actually
read. A register map that cannot state what a value means is not a description of
an interface; it is a list of addresses.

**A field's `property` is now that property's only name.** It is what expressions
reference, what procedures target, and what the reverse-dependency index of D1 is
built from — so nothing about D1 changed, and two fields sharing an identifier is
now the error that "carried by more than one register" used to be.

**What is stated, and what proved derivable.**

- **`access` is stated, and required in every read-write space.** This is the
  entry's most consequential rule and the reason is the specification itself:
  FC 03, FC 06 and FC 16 are defined against the same *table*, but Modbus makes
  no per-address guarantee at all. It leaves the mapping of the data model onto
  device memory entirely to the vendor, so a read-only or write-only holding
  register conforms, and a client cannot discover which by probing. A default of
  `read_write` would therefore be the format asserting something the protocol
  does not — the same objection that made `addressing.base` required in D17. An
  input register is exempt: FC 4 fixes its direction, so stating it says nothing.

  This also retires D16's derivation of register access from property access.
  That derivation measured identical in 209 of 209 cases and was right at the
  time; with the semantic document gone there is nothing left to derive from, so
  the fact has to be stated, and the narrowing check it justified is deleted.

- **`kind` is dropped, having proved derivable.** Setting, measurement or command
  follows from the space and the access: input space or read-only means
  measurement, write-only means command, read-write holding means setting.
  Measured across all 277 carried fields at migration time, the declared value
  agreed in **277 of 277**. Per D16, a fact that follows is not stored;
  `validate.py` derives it for the two warnings that need it.

- **`type` is kept**, though it looks derivable from the presence of `enum`,
  `labels` or `scale`. That derivation is exact on this map and wrong the moment
  a device sends a genuine `float32`, which carries no scale — precisely the trap
  D16 named when it refused to derive `encoding` from `range`.

- **`internal` and `storage` are dropped.** An internal property is one no
  register carries, which is unrepresentable once the map is the only document;
  the two the AHU had are simply gone. `storage` described a firmware struct
  width rather than anything on the wire, and was never used.

**D18 is most of the way paid off as a side effect.** Register titles came across
from the properties that had them, so the 190 registers missing a title and the
87 bit fields missing one are both now zero. 185 still lack a description, which
is the honest remainder: only 24 properties ever carried prose.

**What the validator lost.** Its whole third layer. Cross-document agreement —
every property carried exactly once, every field naming a property that exists —
collapses into a field-index that reports duplicates, because with one document
those cannot disagree. What remains as `check_layout` is what was always local to
the map: address occupancy, bit collisions, wide encodings running into their
neighbours, and whether an encoding can carry the type and range a field claims.
`check_procedures` gained a check the split made impossible to write cleanly: a
procedure that writes a register the bus exposes read-only is now an error.

**Cost.** The map went from 954 lines to roughly 3100, which is the whole point:
it now contains what two files did. Every consumer reads one document, and the
generator lost its second load. The measured casualty list for the 3SModbus
conversion dropped from three to one — the two internal properties stopped
existing, leaving only the three-term conjunction the library's condition grammar
cannot express.

**Left open.** One address meaning different things on read and on write is not
expressible, and Modbus permits it: `OPEN-QUESTIONS.md` Q6 records the shape that
would express it and why it is not being built yet.

## D22 — A field has an `id`, and may occupy a span of bits rather than one

Two changes to the same 277 fields, made together because they touch the same
line.

### `property:` becomes `id:`

The key was a **foreign key whose table was deleted**. It named an entry in
`device-properties.yaml`; D21 removed that file, so the string stopped being a
reference and became the field's own name — and `property:` inside an object that
*is* the property reads like `person: alice` inside a person record.

**Asked directly what the key is for, the honest answer is one application:** it
is the handle that lets one value refer to another without hardcoding an address.

    writable_when: operating_mode == manual

Without it that has to become `HR22 == manual`, which works — it is what the
3SModbus library does — and breaks silently the moment the map is renumbered,
which is the edit D14 measured. Procedure steps are the same case, and D1's
reverse-dependency index is built from those references. The second application is
the firmware X-macro, where the identifier becomes a C token; that one only
matters for our own devices.

So the field earns its place, and `id` names what it is. It also gives the format
a split it lacked: **`id` is for machines, `title` is for humans.** `name:` would
have been wrong, since `exceptions` already uses `name` for a short human label.

### A field may occupy `bits: [low, high]`

Real devices pack several values of different widths into one register — a
communication settings word carrying a transmission mode flag in bit 0, a baud
rate enum in bits 1-4, a parity enum in 5-6, a timeout integer in 7-14 and a flag
in 15. The format could express none of it: `bit: n` covers one bit and only for
a bool.

    fields:
      - { id: transmission_mode, bit: 0, type: bool, labels: { false: rtu, true: ascii } }
      - { id: baud_rate, bits: [1, 4], type: enum, enum: { 0: b4800, 1: b9600 } }
      - { id: parity, bits: [5, 6], type: enum, enum: { 0: none_8n1, 1: even_8e1 } }
      - { id: response_timeout, bits: [7, 14], type: int, unit: "ms", scale: 10 }
      - { id: exception_before_timeout, bit: 15, type: bool }

**The alternative was to enumerate the combinations, and the arithmetic settles
it.** 2 × 16 × 4 × 256 × 2 = **65 536**: every possible value of a 16-bit
register. Enumeration is not a shortcut for this shape, it is a re-encoding of the
entire value space with the structure discarded. Even in a mild case — baud and
parity alone, 64 combinations — it would be wrong: a commissioning tool could not
offer two controls, the labels could not compose, and changing parity would mean
looking up a different combined value instead of writing one field.

`bit: n` stays as shorthand for `bits: [n, n]`, which is the one place this format
knowingly keeps two spellings for one fact. The reason is proportion: 87 of the 88
existing bit fields are single, and `bits: [3, 3]` reads worse than the thing it
describes. `bit_span()` in `validate.py` normalises the two, so exactly one
function knows the difference and every check works in spans.

**What the span makes checkable**, none of which was possible before:

- spans overlapping inside one register, which was previously a per-bit check and
  is now per-range
- an enum with more members than its span holds: `bits: [5, 6]` carries 0..3, so a
  five-member parity enum is a bug the format could not previously see
- a range that does not fit after scale: 8 bits hold 0..255, so a timeout declared
  `[0, 5000]` with `scale: 10` is raw 0..500 and does not fit
- a bool spread over several bits, which is a modelling mistake rather than a wide
  bool
- a **gap** below the highest field, as a warning: an undescribed bit in a packed
  settings word is usually an omission rather than a spare

**Two consequences.** `clear_label` is now restricted to registers whose fields
are *all* single bools: there is no all-clear state to name when one field is an
integer, because zero there is a value rather than an absence. And a register
using spans cannot be converted to the 3SModbus format at all — its bitwise
decoder labels individual bits and nothing else — so the generator reports it
rather than emitting something plausible and wrong.

**A span stays inside one register.** A value crossing into the next address is a
different feature, and mixing the two would make both harder to read; it belongs
with `OPEN-QUESTIONS.md` Q1 if a device ever needs it.

**Not yet exercised by the map.** The AHU has no such register — its baud rate and
parity are separate registers — so this vocabulary is checked by the schema tests
and by validator probes rather than by the document. That is the state the coil
rules were in before D20, and the same caution applies: the first real
multi-value register, ours or a third party's, is what will confirm the shape.
Signed sub-fields are unhandled: a span reads out unsigned, and two's complement
inside a span is not expressible until something needs it.
