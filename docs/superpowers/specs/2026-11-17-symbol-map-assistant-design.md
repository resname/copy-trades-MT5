# Symbol Map Assistant (Example-Based Rule Generator)

Date: 2026-11-17
Status: Approved (design)
Size: Project (GUI dialog + pure inference logic + tests + docs)

## Intent

The symbol map already supports regex rows (`master pattern → slave template`
with `$1..$9`), but writing the pattern and template by hand is error-prone.
The user's futures family is a concrete example: the master trades `YMZ26`
(December 2026), the next contract is `YMH27` (March 2027), while the slave
names the contracts `YM_Z` / `YM_H` (month code, no year). A correct rule is
`^(YM)([FGHJKMNQUVXZ])(\d{2})$ → $1_$2`, but the user should not have to
derive that.

The assistant lets the user enter one or two example pairs
(`YMZ26 → YM_Z`, `YMH27 → YM_H`) in the GUI and generates the rule, with a
live preview. The generated rule is shown in editable fields, so the user can
verify or fix it before adding it to the symbol table.

## Goals

- An **Assistant…** button next to Add Row / Remove Row in the shared
  symbol-map widget (used by both the profile editor and the slave editor).
- Enter one or more master/slave example pairs (two recommended); derive a
  master regex and slave template automatically.
- Show the derived rule in editable fields plus a live preview.
- On apply, insert the pair as one new row in the symbol table.
- Replace an existing row with the same master pattern instead of duplicating
  it.
- Keep the existing map format and engine behaviour unchanged.
- **Single-trade invariant:** one master NEW produces at most one OPEN command
  per slave, no matter how many map rows could match the master symbol.
  Multiple matching rows never multiply trades; the first match wins. The only
  multiplicity stays the existing oversized-volume split, which opens several
  orders on the *same* chosen slave symbol.
- The assistant warns when an existing row would win for the new rule's
  examples (the new rule would be ineffective) and offers to insert above the
  conflicting regex row. An exact-tier row always wins regardless of position,
  so that conflict is reported as not fixable by ordering.

## Non-goals

- Dynamically choosing among slave symbols from the slave's symbol list /
  expiry metadata (the bigger worker/IPC feature discussed separately).
- Changing `SymbolMapper` resolution semantics.
- Editing existing rows in place from the assistant (the user can still edit
  the table cells directly).
- Supporting every possible broker naming scheme. The assistant infers the
  common futures shape; unusual schemes can be fixed in the editable fields.

## Anti-goals

- Silently adding a rule that does not map every entered example to its slave
  example.
- Adding a row that would break the map CSV (`=` or `,` inside a field).
- Changing the engine or any saved config that already exists.

## Approaches considered

1. **Example-based assistant** (chosen): the user supplies example pairs; the
   assistant infers root, month code and optional year, generates
   regex + template, and previews it. Matches the user's "parse symbols in the
   GUI" request and covers broker variants.
2. **Futures form**: fixed fields for root + master format + slave format.
   Deterministic, but only handles contract names and still asks the user to
   understand the structure.
3. **Live tester only**: a preview field under the table; the user still writes
   the regex. Too little help for the stated problem.

The assistant includes the live preview from approach 3, but generates the
rule as in approach 1.

## Constraints

- The symbol map is serialized as `master=slave` pairs joined by commas; the
  map parser strips spaces and skips pairs without exactly one `=`. Generated
  fields must contain neither `,` nor `=`.
- Resolution order: exact entries first, then regex rows in list order, first
  `re.fullmatch` wins, then the same-name fallback. The assistant must place
  the row where the user expects and must not create duplicate master patterns.
- The engine-side `SymbolMapper` is not modified; the generated rule is
  validated by instantiating it with the real mapper.
- The dialog is modeless only within the modal editor flow; it follows the
  existing PySide6 patterns and runs offscreen in tests.

## Design

### Pure inference module

New `manager/engine/symbol_assist.py`:

```python
@dataclass(frozen=True)
class SymbolRule:
    master_pattern: str
    slave_template: str

class SymbolAssistError(ValueError):
    """Inference failed; the message names what is missing."""

def infer_symbol_rule(examples: list[tuple[str, str]]) -> SymbolRule:
    ...
```

Inference, for one or more example pairs:

1. Strip whitespace; require at least one non-empty pair.
2. Master side, per example:
   - Find the **rightmost** month-code letter from `FGHJKMNQUVXZ` that is
     immediately followed by a digit run. The **entire** run must be 2–4
     digits (the contract year); a 1-digit or 5+-digit run is rejected.
     Without such a letter+run, raise "the master example needs a month code
     and a year, e.g. YMZ26" (undated contracts are edited manually).
   - master prefix = everything before that letter (must be non-empty);
     master suffix = everything after the year digits (e.g. `.cfd` in
     `YMH27.cfd`). A root that itself contains a month letter (`YM`) is not
     mistaken for the month because the rightmost match wins.
3. All master examples must agree on the prefix, the year width and the
   suffix. Otherwise raise "the examples describe different symbol shapes".
4. Slave side, per example:
   - Month letter = the **last** month-code letter in the string. If there is
     none, raise "the slave example needs a month code, e.g. YM_Z".
   - slave prefix = everything before it; slave tail = everything after it.
   - If the tail starts with digits whose width equals the master year width,
     those digits are the slave year and the rest of the tail is the slave
     suffix. If the tail starts with digits of a different width, raise "the
     slave year width does not match the master's". If it starts with no
     digits, the slave has no year and the whole tail is its suffix.
   - All slave examples must agree on the prefix, the suffix and whether they
     carry a year (and its width).
5. Build the master pattern:
   `^` + `(` + escaped master prefix + `)` + `([FGHJKMNQUVXZ])` +
   `(\d{n})` + escaped master suffix + `$`.
   Group 1 = master prefix, group 2 = month code, group 3 = year.
6. Build the slave template from the slave example: replace the month letter
   with `$2`, replace the slave year (if any) with `$3`, and keep the slave
   prefix and suffix literally — except that a literal `$` in the slave text
   must be written `$$`, because `_expand_template` treats `$n` as a
   backreference and `$$` as a literal `$`. (`YMZ26 → YM_Z` gives `YM_$2`;
   `YMH27.cfd → YM_H.cfd` gives `YM_$2.cfd`.)
7. Validate with `SymbolMapper(f"{pattern}={template}", lambda s: False)` —
   the same-name fallback is disabled so an invalid rule cannot look valid.
   Every master example must resolve to its slave example; otherwise raise
   `SymbolAssistError` with a plain-language reason.

Known limitations, surfaced in the dialog, not errors:
- The master example must carry a year; undated contracts are not inferred
  (the editable rule fields remain the escape hatch).
- The slave must contain the same month-code letter as the master.
- One month code per symbol; no numeric month representation.
- A master **or slave** suffix that itself contains a month-code letter can
  confuse the rightmost-month rule; with a single example this can even
  validate and generate a wrong template. The editable fields are the escape
  hatch.
- Roots may differ between master and slave (`NQH27 → NAS_H`): the master root
  is captured in group 1, the slave root is written literally.

### Dialog

New `manager/gui/symbol_assistant.py`, class `SymbolAssistant(QDialog)`:

The app's GUI and README are English; keep every new user-facing string
English for consistency. The button label is **Assistant…** (with the ellipsis
used by the other dialogs).

```
Symbol Assistant
─────────────────────────────────────────────────
Examples (master → slave)
  [ YMZ26      ]  →  [ YM_Z        ]   [−]
  [ YMH27      ]  →  [ YM_H        ]   [−]
  [+ Example]

Rule (editable)
  Master:  [ ^(YM)([FGHJKMNQUVXZ])(\d{2})$ ]
  Slave:   [ YM_$2                         ]
  [ Re-derive ]

Preview
  Test master symbol: [ YMZ26     ]  →  YM_Z
  ✓ matches example 1

[ Cancel ]                          [ Apply ]
```

- Examples table: two editable columns, add/remove buttons, starts with one
  empty row.
- Inference runs automatically when an example changes and the rule fields
  have not been manually edited. Editing a rule field stops auto-inference;
  **Re-derive** re-runs it.
- Preview updates live: it resolves the test master symbol (default: the first
  example) with the current rule fields and reports either the result or the
  validation error.
- Apply validates:
  - at least one complete example,
  - the master pattern compiles and contains no `,` or `=`,
  - the slave template contains no `,` or `=`,
  - every example resolves to its slave example.
  On failure the dialog stays open with an inline message. On success it
  returns:
  ```python
  @dataclass(frozen=True)
  class AssistantResult:
      master_pattern: str
      slave_template: str
      insert_before: int | None = None   # index into the existing rows
  ```
- **Shadowing check (computable direction only):** the dialog receives the
  profile's current rows in order (`existing_rows: list[tuple[str, str]]`).
  Before applying, it resolves every example through those rows alone with
  `exists_check=lambda s: False`. If the result is not the new rule's expected
  slave name, the new rule would be ineffective for that example; the dialog
  names the winning row and why:
  - If the example equals an existing row's master text **literally**, that
    row is in the exact tier and wins regardless of position. Apply anyway, or
    edit/remove that row; moving the new row above it cannot help.
  - Otherwise the winning row is the first matching regex row at index `i`;
    the dialog offers **Insert above**, which returns `insert_before=i`.
  With several conflicting regex rows, `insert_before` is the lowest of their
  indices, so the new row precedes all of them. Exact-tier conflicts are
  listed separately because ordering does not solve them.
  The reverse direction ("the new rule would shadow some existing row") is not
  claimed: existing rows have no example pairs, and arbitrary regex-language
  overlap is not computable in practice.

### Widget integration

`CopySettingsWidget` (shared by `ProfileEditor` and `SlaveEditor`):
- New `assistant_button = QPushButton("Assistant…")` next to Add Row /
  Remove Row. Add it to `set_editable`, so the button is disabled whenever the
  symbol table is locked (a profile-attached slave's read-only preview).
- New `symbol_map_pairs() -> list[tuple[str, str]]` returns the current rows
  in order (the same cells `_symbol_map_csv()` serializes). `_symbol_map_csv()`
  itself is unchanged, so saving/loading needs no changes.
- On click, open `SymbolAssistant(existing_rows=self.symbol_map_pairs(),
  parent=self)`. On accept:
  - If `insert_before` is not `None`, insert the pair at that index and select
    it.
  - Else if a row with the same master pattern exists, replace the **first**
    such row's slave cell and remove any additional rows with the same master
    pattern (otherwise a later duplicate would win in the engine).
  - Otherwise append a new row and select it.
- The `set_editable` change is asserted in
  `test_copy_settings_widget_set_editable_disables_everything`.

### Single-trade invariant (already true, now pinned)

- `SymbolMapper.resolve(master_symbol)` returns **one** slave name: exact map
  entry first, then the first regex row whose `fullmatch` succeeds, then the
  same-name fallback. There is no code path that returns a list of symbols.
- `derive_command` therefore emits at most one OPEN command per master NEW per
  slave. A second matching row is never evaluated for that symbol.
- The worker's oversized-volume path (`split_volume`) may open several orders,
  but always on the chosen symbol; it never consults the symbol map again.
- The assistant only changes which single row wins (by warning about shadowing
  and ordering); it cannot make one master trade resolve to multiple symbols.

### Backward compatibility

- Existing symbol maps and the engine are untouched.
- The assistant only inserts a row the user explicitly accepted.
- Old saved configs load exactly as before.

## Edge cases

- A month code that is also part of the root (e.g. `YMM27` for June) is fine:
  inference uses the rightmost month-code letter followed by the year, so the
  root's `M` is not mistaken for the month.
- Broker prefixes/suffixes are preserved literally: `YMH27.cfd → YM_H.cfd`
  yields the pattern `^(YM)([FGHJKMNQUVXZ])(\d{2})\.cfd$` and the template
  `YM_$2.cfd`.
- A slave name that includes the year (`YM_H27`) gets `$3` in place of the
  year digits; its year width must match the master's.
- A 4-digit master year (`YMZ2026`) is supported; the width must be consistent
  across examples.
- Master and slave roots may differ (`NQH27 → NAS_H`): group 1 captures the
  master root, the slave root is literal.
- Fewer than two examples are allowed; a single pair still generates a rule,
  but a second example is recommended because it is the only cross-check that
  the inferred shape is not a coincidence.
- Inconsistent examples (different master prefixes/suffixes, different year
  widths, different slave prefixes/suffixes or year presence) are rejected
  with a clear message; the user can edit the rule fields manually.
- If the generated pattern matches more broadly than intended, the preview and
  the editable fields make that visible before applying.

## Testing

- `manager/tests/test_symbol_assist.py`: pure inference.
  - `YMZ26→YM_Z`, `YMH27→YM_H` → `^(YM)([FGHJKMNQUVXZ])(\d{2})$` / `YM_$2`;
    the generated mapper resolves both examples and `YMM27 → YM_M`.
  - Other families (`ESZ26→ES_Z`), a different slave root
    (`NQH27→NAS_H`), broker suffix (`YMH27.cfd→YM_H.cfd` with the escaped
    suffix in the pattern and `YM_$2.cfd` as the template), a slave with a
    year (`YMH27→YM_H27`), a 4-digit master year, a root containing the month
    letter (`YMM27→YM_M`).
  - Failure cases: no trailing year on the master, a 1-digit or 5+-digit year
    run, no month code, empty master prefix, mixed year widths, inconsistent
    prefixes/suffixes, slave missing the month letter, slave year width
    mismatch, empty input.
  - Validation disables the same-name fallback: a same-name example whose
    rule does not actually match is rejected (it would "resolve" with the
    fallback enabled).
  - A literal `$` in the slave text is emitted as `$$` in the template and
    survives `_expand_template`.
  - CSV safety: the generated pattern/template contain no `,` or `=`.
- `manager/tests/test_symbol_assistant.py` (GUI, offscreen):
  - Filling two examples auto-fills the rule and preview.
  - Manual rule edit stops auto-inference; **Re-derive** restores it.
  - Apply appends/replaces the table row and serializes into `symbol_map_csv`.
  - Accepting returns an `AssistantResult`; with a regex-row conflict,
    **Insert above** sets `insert_before` to the lowest conflicting index and
    the widget inserts there, so the new rule wins.
  - An exact-tier conflict (an existing row's master text equals an example)
    is reported as such; inserting above does not change the winner, and Apply
    anyway appends with the exact row still winning.
  - Duplicate master patterns: accepting a same-master rule replaces the first
    matching row and removes the duplicates.
  - Invalid examples keep the dialog open.
  - Cancel changes nothing.
- `manager/tests/test_profile_editor.py` / `test_slave_editor.py`: the
  assistant button exists, is disabled by `set_editable(False)`, and the
  applied row round-trips through `values()`/`set_values()`.
- Single-trade invariant regression tests:
  - `test_transform.py`: a map with two overlapping matching rows resolves a
    master symbol to exactly one slave name (the first row's).
  - `test_copy_loop.py`: ingesting one master NEW through a slave whose map has
    overlapping rows emits exactly one OPEN command.
  - `manager/tests/test_mt5_worker.py` / existing split-order tests stay green:
    an oversized volume still opens multiple orders on the one resolved
    symbol.
- `README.md`: document the assistant and the YM example, and state the
  one-trade-per-master-trade-per-slave rule with the split-order exception.

## What is left to the builder

- Exact dialog layout (spacing, labels) within the existing Qt style.
- The precise wording of validation messages.
- Whether the preview also lists the other examples' results (recommended:
  one line per example with a check mark).
- Any additional inference heuristics beyond the documented shape, as long as
  the documented tests pass.
