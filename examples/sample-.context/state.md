# Project state

## Now
- **Focus:** CSV export endpoint for the reports page
- **Next action:** stream rows instead of buffering — current impl OOMs on >50k rows
- **Branch:** feat/csv-export · **Status:** in-progress

## Active constraints
- exports must stream (no full-result buffering)
- every endpoint goes through the existing `withAuth` middleware

## Open threads
- decide CSV vs XLSX default for the "Download" button

## Pointers
- Durable repo truths → durable/manifest.md
