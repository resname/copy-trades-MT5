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

- A **Symbol-Assistent…** button next to Add Row / Remove Row in the shared
  symbol-map widget (used by both the profile editor and the slave editor).
- Enter 1–2 master/slave example pairs; derive a master regex and slave
  template automatically.
- Show the derived rule in editable fields plus a live preview.
- On apply, insert the pair as one new row in the symbol table.
- Replace an existing row with the same master pattern instead of duplicating
  it.
- Keep the existing map format and engine behaviour unchanged.

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
2. Master side: split trailing digits (`(\d+)$`) as the optional year. The
   remaining head must end with a month-code letter from
   `FGHJKMNQUVXZ`; the master prefix is everything before that letter. All
   examples must agree on the year-digit count (or all have none).
3. Slave side: if the master has a year and the slave ends with the same
   number of digits, treat those as the slave's year; otherwise the slave has
   no year. Locate the month-code letter in the slave core (after the slave's
   own prefix), replace it with the month group, and the trailing year digits
   with the year group.
4. Build the master pattern:
   `^` + escaped master prefix + `([FGHJKMNQUVXZ])` +
   (`(\d{n})` if a year exists) + `$`.
   Group 1 = master prefix, group 2 = month code, group 3 = year (if present).
5. Build the slave template from the slave example: replace the month letter
   with `$2`; if the slave has a year, replace its digits with `$3`. Because
   group 1 is the master prefix, the template does not need `$1` unless the
   slave text includes the master prefix; inference instead keeps the literal
   slave prefix. (For `YMZ26 → YM_Z`, the template is `YM_$2`.)
6. Validate by constructing `SymbolMapper(f"{pattern}={template}", lambda s:
   True)` and resolving every master example; each must equal its slave
   example. On mismatch or structural inconsistency, raise
   `SymbolAssistError` with a plain-language reason (e.g. "the slave example
   does not contain the month letter").

Known limitations, surfaced in the dialog, not errors:
- The slave must contain the same month-code letter as the master.
- One month code per symbol; no numeric month representation.
- Mixed year formats across examples are rejected.

### Dialog

New `manager/gui/symbol_assistant.py`, class `SymbolAssistant(QDialog)`:

```
Symbol-Assistent
─────────────────────────────────────────────────
Beispiele (Master → Slave)
  [ YMZ26      ]  →  [ YM_Z        ]   [−]
  [ YMH27      ]  →  [ YM_H        ]   [−]
  [+ Beispiel]

Regel (editierbar)
  Master:  [ ^(YM)([FGHJKMNQUVXZ])(\d{2})$ ]
  Slave:   [ YM_$2                         ]
  [ Neu ableiten ]

Vorschau
  Test-Master-Symbol: [ YMZ26     ]  →  YM_Z
  ✓ passt zu Beispiel 1

[ Abbrechen ]                       [ Übernehmen ]
```

- Examples table: two editable columns, add/remove buttons, starts with one
  empty row.
- Inference runs automatically when an example changes and the rule fields
  have not been manually edited. Editing a rule field stops auto-inference;
  **Neu ableiten** re-runs it.
- Preview updates live: it resolves the test master symbol (default: the first
  example) with the current rule fields and reports either the result or the
  validation error.
- Apply validates:
  - at least one complete example,
  - the master pattern compiles and contains no `,` or `=`,
  - the slave template contains no `,` or `=`,
  - every example resolves to its slave example.
  On failure the dialog stays open with an inline message; on success it
  returns `(master_pattern, slave_template)`.

### Widget integration

`CopySettingsWidget` (shared by `ProfileEditor` and `SlaveEditor`):
- New `assistant_button = QPushButton("Assistent…")` next to Add Row /
  Remove Row.
- On click, open `SymbolAssistant(parent=self)`; on accept:
  - If a row with the same master pattern exists, replace its slave cell.
  - Otherwise append a new row and select it.
- The existing `_symbol_map_csv()` serialization is unchanged, so saving and
  loading a profile need no changes.

### Backward compatibility

- Existing symbol maps and the engine are untouched.
- The assistant only inserts a row the user explicitly accepted.
- Old saved configs load exactly as before.

## Edge cases

- A month code that is also the root's last letter (e.g. `YM_M` for June) is
  fine because inference strips the master prefix before locating the month.
- Broker prefixes/suffixes are preserved literally (e.g. `YMH27.cfd → YM_H.cfd`
  yields `$(...)_$2.cfd`).
- A slave name that includes the year (`YM_H27`) gets `$3` in place of the
  year digits.
- Fewer than two examples are allowed; a single pair still generates a rule
  (the family shape is inferred from that one pair).
- Inconsistent examples (different roots, different year widths) are rejected
  with a clear message; the user can edit the rule fields manually.
- If the generated pattern matches more broadly than intended, the preview and
  the editable fields make that visible before applying.

## Testing

- `manager/tests/test_symbol_assist.py`: pure inference.
  - `YMZ26→YM_Z`, `YMH27→YM_H` → `^(YM)([FGHJKMNQUVXZ])(\d{2})$` / `YM_$2`;
    the generated mapper resolves both examples and `YMM27 → YM_M`.
  - Other families (`ESZ26→ES_Z`, `NQH27→NQ_H`), broker suffix
    (`YMH27.cfd→YM_H.cfd`), slave with year (`YMH27→YM_H27`), root whose last
    letter is the month (`YMM27→YM_M`).
  - Failure cases: inconsistent roots, no month code, no trailing year on the
    master, mixed year widths, empty input, slave missing the month letter.
  - CSV safety: the generated pattern/template contain no `,` or `=`.
- `manager/tests/test_symbol_assistant.py` (GUI, offscreen):
  - Filling two examples auto-fills the rule and preview.
  - Manual rule edit stops auto-inference; "Neu ableiten" restores it.
  - Apply inserts/replaces the table row and serializes into `symbol_map_csv`.
  - Invalid examples keep the dialog open.
  - Cancel changes nothing.
- `manager/tests/test_profile_editor.py` / `test_slave_editor.py`: the
  assistant button exists and the applied row round-trips through
  `values()`/`set_values()`.
- `README.md`: document the assistant and the YM example.

## What is left to the builder

- Exact dialog layout (spacing, labels) within the existing Qt style.
- The precise wording of validation messages.
- Whether the preview also lists the other examples' results (recommended:
  one line per example with a check mark).
- Any additional inference heuristics beyond the documented shape, as long as
  the documented tests pass.
