# What is missing from the protocol document

An audit of `device-modbus.yaml` and `device-modbus.schema.json` against the
question D15 left them holding: *is this a complete description of the device's
access API?*

Method, as everywhere else here: measure first. Every claim below was checked
against the two documents, the validator, the generator, or a mutation probe
that was actually run. Findings are split by who can close them — a format
defect I can fix, or a firmware fact only the device author knows.

**Status.** A1, A2 and A4 are closed by D17, which also added `identification:`
for device detection; A5 went with them. D19 made FC 43 declarable and gave
`limits` its first reader, which does not close A3 but ends its decorativeness.
D20 added `exceptions:` and closed B5, and gave `limits` two more readers — with
D19's that is three, so A3's "decorative" charge no longer holds even though its
remaining keys are still unread. A6, A7, B1–B4, B6 and B7 are still open.

This audit also **missed one**, recorded as D18: no register carried a title or
a description except the 19 the schema forced, because the audit asked what a
client needs in order to talk to the device and never what a reader needs in
order to understand it. 190 registers and 87 bit fields are short of prose.

Evidence is from the AHU map (209 registers) and from a second real device,
`DSCDG3-4` (a duct sensor, 33 input + 43 holding registers), whose
commissioning-tool profile predates all of this.

## Summary of what is measured, not asserted

| Vocabulary | Declared | Used in the AHU map | Consumed by a tool |
|---|---|---|---|
| `space: holding` / `input` | yes | 130 / 79 | yes |
| `space: coil` / `discrete` | yes | **0** | no |
| `encoding: uint16` / `int16` / `bit` | yes | 145 / 45 / 87 | yes |
| `encoding: uint32/int32/uint64/int64/float32/float64/chars` | yes | **0** | partly |
| `count`, `word_order` (field) | yes | **0** | no |
| `reserved` ranges | yes | **0** (23 ranges still unaccounted) | no |
| `access` (narrowing) | yes | 0, by design (D16) | yes |
| `clear_label` | yes | 19 | yes |
| `limits.*` | yes | 5 keys set | **nothing reads it** |

Two thirds of the encoding vocabulary, both bit-oriented address spaces, and
the whole `limits` block have never been exercised by a real document or read
by a real consumer. D15 asserted that 1:N mapping is "implied by `uint32`,
`float32` and `chars`" — the map uses none of the three, so that path is
claimed rather than demonstrated.

---

## A. Format defects — closeable without asking the firmware

### A1. The addressing base is never stated, and it is the one off-by-one that matters

**CLOSED by D17.**

`address: 1` does not say whether it is a 1-based data-model register number
(what a datasheet prints, what `DSCDG3-4`'s profile calls `"number": 1`) or a
0-based PDU offset (what goes on the wire). `generate_library_json.py` maps
`reg["address"] -> "number"` **1:1**, which silently commits to the first
reading; the schema meanwhile permits `address: 0`, which only makes sense
under the second.

Probe: a register at `address: 0` is accepted by the schema and by all three
validator layers.

Three consumers have to agree on this, and a firmware X-macro is the one that
must subtract the base. Until it is declared, the documents are one convention
change away from being wrong everywhere at once, with no diff to show it.

**Needs:** a required top-level declaration of the base, and an address
`minimum` that follows from it.

### A2. A coil is a single bit, and the schema does not know that

**CLOSED by D17.**

Probes, all **accepted** by schema and validator together:

- a `coil` register carrying `encoding: uint16`
- a `coil` register carrying a field with `bit: 3` — in coil space the address
  *is* the bit, so a bit position is meaningless
- a `discrete` register carrying `float32`

`coil` and `discrete` are declared vocabulary with zero uses and zero checks,
so the format currently promises support for bit-oriented spaces while
accepting physically impossible maps in them. Either the spaces get their
constraint (exactly one field, encoding `bit`, no `bit:` key) or they should
come out of the enum until a device needs them.

### A3. `limits` is decorative

`supported_fc`, `max_read_words`, `max_write_words`, `gaps_readable` and
`turnaround_ms` are declared and set. Nothing reads them: 0 references in
`validate.py`, 0 in `generate_library_json.py`. The map uses `holding` and
`input`, consistent with `supported_fc: [3, 4, 6, 16]` — but nothing checks
that, and a map that added a coil register would keep validating against a
device that declares no coil function code.

Cross-checks that the declaration makes possible and nobody performs:

- every `space` in the map must be reachable by some declared function code
  (coil needs 1 and/or 5/15; discrete needs 2)
- a field whose encoding spans more registers than `max_read_words` can never
  be read in one transaction
- `gaps_readable: false` means a client may not span an unoccupied address, so
  the map's gaps become part of the read-blocking plan a commissioning tool
  builds

Unread declarations rot. This is the same failure D16 named for register
`access`, in the other direction: there, one fact was stored twice and a
checker stood in for a single source; here, a fact is stored once and never
used at all.

### A4. The protocol document does not say which device it describes

**CLOSED by D17.**

`device-properties.yaml` opens with `device:` — name, description, type 4010,
firmware_version 1.0. `device-modbus.yaml` opens with `limits:`. Its top-level
keys are exactly `['limits', 'registers']`: an anonymous list of addresses.

Nothing binds a register map to the device and firmware version whose map it
is. D14's own churn measurement was about firmware versions of one device, and
D15 split the documents on the argument that a second interface could be added
later — both make the binding load-bearing, and it is absent. `DSCDG3-4`'s
profile carries `deviceName`, `deviceType` and `firmwareVersion` for exactly
this reason.

**Needs:** a required header naming the device, its type and the firmware
version range this map is valid for, and a layer-3 check that it agrees with
the properties document.

### A5. `count` is accepted on encodings that cannot have a count

**CLOSED by D17.**

Probe: `{ property: …, encoding: uint16, count: 4 }` is accepted by the schema.
It only produced an error incidentally, by colliding with the next register's
span. `count` is documented as "element count for an array or a string" and
belongs to `chars` and to arrays, which the map does not have.

### A6. Character packing density is unstated

`validate.py` sets `SPAN["chars"] = 1`, i.e. one character per register, which
wastes a byte per register. `DSCDG3-4` packs two bytes per register in its
`bytewise` registers. With no `text` property and no `chars` field anywhere in
the AHU, the assumption is untested and undocumented. Whichever it is, it is a
wire fact and belongs in the protocol document.

### A7. `byte_order` and `word_order` are in the wrong box

Both sit under `limits`, which the schema describes as "what this device
tolerates, as opposed to what Modbus permits". Endianness is not a tolerance,
it is the wire format. Modbus already fixes the byte order *within* a register
as big-endian, so `byte_order` only has meaning for `chars`/byte-packed values,
while `word_order` only has meaning for encodings wider than one register. The
grouping hides which key applies to what, and neither is exercised.

---

## B. Protocol facts that only the firmware author can supply

These have **no vocabulary at all** in the schema. Each is a question a
commissioning tool or an integrator asks, and the format currently cannot
answer it. They are listed as questions because the answers are yours.

### B1. What happens when a client writes out of range?

`range` lives in the semantic document. The protocol document never says
whether the device clamps to the limit, rejects with exception 03, or accepts
the value. A commissioning tool needs this to know whether to pre-validate;
documentation needs it to be honest; firmware needs it to be implemented one
way.

### B2. When does a write take effect?

`server_baud_rate` (HR2) almost certainly does not change the running link
mid-transaction, and the map holds `modbus_registers_reset` (HR10) alongside
it — but nothing states "this register takes effect after a reset" as opposed
to immediately. `DSCDG3-4` has the same pair, with the same silence.

### B3. Which registers commit to flash, and how often may they be written?

The AHU declares an internal `nvm_write_count` property, so writes hit
non-volatile memory with a finite endurance. Nothing marks *which* registers
those are, so nothing stops a client polling a setting in a loop and wearing
the part out. This is a protocol-level warning with a real physical cost.

### B4. Must a multi-register value be written atomically?

A `uint32` or `float32` spans two registers. Writing them as two FC6
transactions leaves the device holding half a value for a moment. `max_write_words`
bounds a transaction's size but says nothing about a field that *must* travel
in one. Unstated for every wide encoding — which is currently all of them,
since none is used.

### B5. Which exceptions does the device raise, and when?

**CLOSED by D20** — `exceptions:` now holds the codes, their per-device meaning,
an explicit `overloads` flag for a standard code used to mean something else,
and prose for proprietary codes. `gaps_readable` is cross-checked against code
0x02 rather than standing in for it. The AHU's six entries are all assumptions.

`gaps_readable: true` is a boolean answer to one corner of this (does reading
an unoccupied address raise exception 02?). Nothing covers illegal function,
illegal data value, server device failure, or busy. An integrator reading this
document cannot tell a rejected write from a broken bus.

### B6. Is the broadcast address supported, and is there a recovery address?

Slave address 0 is a broadcast write in Modbus. The map holds
`server_slave_address` as a register, and the address range is derivable from
that property, so per D16 it should not be restated — but broadcast support
and any fixed service address for reaching a misconfigured device are not
derivable from anything.

### B7. How long is the device unresponsive after a reset or an NVM commit?

`turnaround_ms: 5` covers ordinary transaction pacing. A device reset, a
factory reset, or a flash commit takes considerably longer, and the procedures
in the semantic document already hard-code guesses at it: `delay_ms: 1000`
after the air-chain swap, a 15-second timeout on the sensor rescan. Those
numbers are protocol facts about this device that currently live as literals
inside individual procedures.

---

## What I would do next

A1 through A5 are defects in the format and can be closed now; they need
decisions about spelling, not knowledge about the device. A6 and A7 are small
and go with them.

B1 through B7 need vocabulary added to the protocol schema and then values that
only you can confirm. The existing `ASSUMPTIONS` comment block at the top of
`device-modbus.yaml` is the right precedent: add the keys, fill in what is
known, and mark the rest as needing confirmation rather than guessing.

The cheapest real test of all of it is `DSCDG3-4`: it is a second device with a
ground-truth commissioning profile, so modelling it exercises `reserved` ranges
(11 unoccupied ranges in its holding map, 11 in its inputs — the same problem
the AHU has 23 of) and the packed-version `chars`/`bytewise` path that the AHU
never touches.
