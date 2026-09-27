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
