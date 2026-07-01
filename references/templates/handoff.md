# Handoff

<!--
The reactivation prompt. `celeborn handoff` regenerates this from state.md + session.json.
Its job: let a brand-new thread (zero history) resume correctly and cheaply.
Keep it to what a fresh agent needs in its first 30 seconds.
-->

**Branch:** <branch> · **Status:** <green | blocked>
**Focus:** <current focus>
**Next required action:** <the single next step>

**Open risks:**
- <anything that could bite the next session>

---

### Resume prompt (paste into a fresh thread)

> Read `.context/state.md`, `.context/session.json`, and `.context/durable/manifest.md`, then
> continue from the Next required action above. If several agents share this repo, check
> `.context/tasks.md` (or `celeborn tasks`) for in-flight cards before claiming new work.
> Run `celeborn search "<topic>"` for anything older. Do not re-do completed work (see `journal.md`).
