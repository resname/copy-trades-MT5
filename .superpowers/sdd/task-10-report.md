# Task 10 Report: README installation and usage guide

## What I implemented

1. Replaced the `## Installation` TODO placeholder in `README.md` with the six-step installation guide specified in the task brief.
2. Appended the `### Example slave configuration` subsection under `## Configuration`, including the table and the lot-size calculation example.

## What I tested and test results

- Rendered `README.md` to HTML using Python-Markdown to verify it parses correctly as Markdown.
- Result: Rendered successfully (55 lines of HTML output; 10 structural tags for tables/lists/headings detected).
- Verified the rendered output contains the expected `## Installation` ordered list and the new example configuration table.

## Files changed

- `/home/a/copy-trades-MT5/README.md`

## Self-review findings

- The installation steps match the task brief verbatim.
- The example slave configuration table and calculation are placed directly under `## Configuration`, before `## Manual Testing Checklist`, which is the natural location.
- No TODO placeholders remain in the modified sections.
- Markdown renders cleanly with no syntax errors.

## Issues or concerns

- None.
