# Open questions

What is undecided, in one place. A decision that has been made lives in
`DECISIONS.md`; a defect that has been measured lives in `PROTOCOL-GAPS.md`.
This file is the index of what neither of those has settled.

## Q1 — Combined registers: how a value wider than one register is described

**The format has an answer and it is ambiguous in two places.** The entry is the
value's *first* register, and the encoding says how far it reaches:

    - address: 100
      fields: [ { property: flow_rate, encoding: float32 } ]     # occupies 100, 101

    - address: 200
      title: "Serial Number"
      fields: [ { property: serial_number, encoding: chars, count: 16 } ]

The framing is deliberate and worth keeping: a 32-bit float across two registers
is **one property in one field spanning two addresses**, not two registers
combined. That is the 1:N direction D15 built `fields` to express, and D16's rule
that encoding is stated rather than derived from a range is what stops the wire
format shifting when a range is widened.

Three things are unsettled.

**(a) Span is implicit in the encoding.** A reader has to know `float32` = 2
registers, `uint64` = 4. Nothing in the document says so; `validate.py` holds a
`SPAN` table and the schema only hints at it in prose. The options are to leave it
derived and publish the table properly, or to state an explicit width per field.
Deriving is the D16-consistent answer — the span follows from the encoding for
every fixed-width type, so storing it would be a second copy of one fact — but
then generated documentation has to print the occupied range, because a datasheet
reader should not have to do the arithmetic.

**(b) Character packing density is undefined, and the current assumption is
almost certainly wrong.** `validate.py` sets `SPAN["chars"] = 1`: one character
per register, wasting the high byte. The DSCDG3-4 profile packs two bytes per
register in its `bytewise` registers, which is the normal Modbus convention. So
`count: 16` today means 16 registers and 16 characters, when it should probably
mean 8 registers and 16 characters. This is gap A6 in `PROTOCOL-GAPS.md` seen
from the other side.

Proposed: fix the density at **two bytes per register, high byte first**, make
`count` mean characters, and let span be `ceil(count / 2)`. `byte_order` then
exists for the devices that swap them, and a trailing odd byte needs a stated
rule (pad with NUL, or with a space, as several vendors do).

Nothing currently exercises any of this: the AHU map has no `chars` field and no
`text` property at all, so the whole path is asserted rather than demonstrated.

**(c) Word order has a home but no test.** Which register holds the high half is
`word_order`, on the field or defaulted per document. This is the thing that
actually bites integrators — ABCD versus CDAB — and it is unexercised here for
the same reason as (b).

**What the format cannot express at all:** a value whose halves are not adjacent,
high word at holding 100 and low word at holding 200. It happens, mostly in
gateway-flattened maps. A single start address plus a derived span cannot say it.
The escape, if one is ever needed, is an explicit word list on the field —
`words: [100, 200]` — at which point the scalar `address` becomes shorthand for
the contiguous case. Not worth building before a device demands it, but it is the
one case that would force `address` to become an object rather than a number.

## Q2 — Expression grammar

`DECISIONS.md` D3, still open, and now the oldest unsettled thing here. D1 made
expressions load-bearing, because the reverse-dependency index is only as good as
what can be extracted from them, and `validate.py` still approximates with a
regex that strips attribute tails and subtracts a keyword list.

Measured on the properties document as it stands: 184 expressions, 72 distinct,
and every one of them is a flat conjunction of four atom shapes —
`property`, `not property`, `property == literal`, `property != literal`. No
`or`, no parentheses, no nesting, no arithmetic, and no quantifier. D3 listed the
quantifier form `any(channel[*].alarm_active)` as the hard part; D4 removed the
channel dimension and D6 replaced the sketch that used it, so the hardest
question in D3 has been moot for thirteen decisions and nobody noticed.

## Q3 — Protocol facts only the firmware author can supply

`PROTOCOL-GAPS.md` section B, of which B5 is closed. Still open: what happens on
an out-of-range write (B1 — the profile now *claims* rejection rather than
clamping, which needs confirming), when a write takes effect (B2), which
registers commit to flash (B3), whether wide values must be written atomically
(B4 — related to Q1), broadcast and recovery addressing (B6), and how long the
device is unresponsive after a reset (B7, currently living as hard-coded
`delay_ms` literals inside individual procedures).

## Q4 — Format defects not yet closed

`PROTOCOL-GAPS.md` A3, A6 and A7: `limits` keys that nothing reads
(`max_read_words`, `max_write_words`, `turnaround_ms`), character packing density
(A6, which is Q1b above), and `byte_order` / `word_order` being filed as things
the device "tolerates" rather than as the wire facts they are.

## Q5 — Register prose

`DECISIONS.md` D18 states the rule and the map does not yet meet it: 190
registers need a title and a description, and 87 bit fields need a title. It is
a validator warning rather than a schema requirement until the count reaches
zero. Not a question about the format — a question about who writes the prose.

## Q6 — Read and write at one address meaning different things

The Modbus specification defines FC 03, FC 06 and FC 16 against the same named
table -- *Holding Registers* -- so at the level of the abstract table they
address the same thing. It makes **no per-address guarantee**: the mapping of the
data model onto device memory is explicitly left to the vendor, and the four
tables may be separate blocks or overlay one another. Three things follow that
are widely assumed to be guaranteed and are not:

- a holding register need not be both readable and writable
- read-back need not equal what was written; FC 16's response echoes the address
  and the quantity, never the stored value, so the protocol confirms nothing
- nothing forbids one address meaning different things in the two directions --
  reading a status word where writing triggers a command

The first is handled: `access` is stated per register, and D21 made it required
in the read-write spaces for exactly this reason. The third is **not
expressible**: a register entry has one `fields` list, so one address has one
meaning, and a device that returns status on FC 03 while accepting a command on
FC 06 would have to be described as one or the other. `modbus_registers_reset`
at holding 10 is a mild instance -- writing `reset` triggers, reading almost
certainly returns `idle` rather than the last value written.

The shape that would express it is a per-direction field list -- `on_read:` and
`on_write:` beside the flat `fields:`, which stays as shorthand for "both". It is
not worth building before a device needs it: it doubles the shape of the
commonest entry in the format. Recorded because the reason it is absent should be
a decision rather than an oversight.

A related consequence that is already handled: because a write response confirms
nothing, a procedure's `verify` step has to re-read the register rather than trust
the write's reply. Every `verify` in the map does.
