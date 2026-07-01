# Journal

<!--
Chronological proof of what happened. Append one entry per meaningful unit of work.
Oldest first, newest at the BOTTOM (the tail is the recent history).
When this file grows past the threshold, `celeborn archive` moves the oldest entries
into journal-archive/ — they stay searchable but leave the Hot/Warm path.

Entry format (copy this):

## YYYY-MM-DD HH:MM — <verb> <what changed>
- **Did:** <what you actually did>
- **Result:** <evidence — file:line, command output, commit SHA, test counts>
- **Next:** <the immediate next step, if any>
- **Tags:** <optional #tags for search>
-->

## 2026-05-30 14:10 — Add /api/reports/export skeleton
- **Did:** added the route + auth, returns 200 with a stub CSV
- **Result:** smoke test green; `git log` 9f2c1a1
- **Next:** real query + streaming

## 2026-06-01 11:25 — Hit OOM on large exports
- **Did:** loaded full result set into memory before serializing
- **Result:** node heap OOM at ~50k rows; reproduced in test `export.oom.test.ts`
- **Next:** switch to a streaming serializer
- **Tags:** #performance #regression
