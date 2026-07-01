"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { motion, MotionConfig } from "motion/react";
import { useJsonPoll } from "@/lib/poll";
import type { Task, TaskBoard, TaskState } from "@/lib/tasks";
import { sortByColumnRules } from "@/lib/sort";
import { SandProgress, type SandStatus } from "@celeborn/board-ui";
import { band } from "@/lib/band";
import type { ActiveAgents as ActiveAgentsData } from "@/lib/agents";

// Card-movement signal (t48). Earlier builds tried to show the move with motion — sliding the cards
// below up into the gap, wiggling the arriving card — but with everything in motion at once the eye
// couldn't tell WHICH card actually moved. So the cards now snap to their new positions instantly and
// the moved card alone gets a soft blue EDGE GLOW that fades to nothing over 5 seconds. Nothing else
// animates; your attention lands on the one card that changed and the glow quietly clears itself.
// `GLOW_ON`/`GLOW_OFF` share the same two-layer box-shadow shape so motion can tween between them.
// A user who prefers reduced motion still sees the glow snap on and clear (MotionConfig only gates the
// tween) — it carries no motion, just colour.
const GLOW_ON = "0 0 0 2px rgba(59,130,246,0.75), 0 0 18px 4px rgba(59,130,246,0.55)";
const GLOW_OFF = "0 0 0 0px rgba(59,130,246,0), 0 0 0px 0px rgba(59,130,246,0)";
const GLOW_TRANSITION = { duration: 5, ease: "easeOut" } as const;

const STATE_META: Record<TaskState, { label: string; accent: string }> = {
  todo: { label: "To Do", accent: "var(--todo)" },
  doing: { label: "Doing", accent: "var(--doing)" },
  done: { label: "Done", accent: "var(--done)" },
};

const POLL_MS = 4000;

/** The prompt text a card sends/copies — mirrors the CLI's `_task_prompt` (title, notes, protocol,
 *  then a project-qualified marker). Pasting into a model lets the hook claim the card in the right repo. */
function taskPrompt(task: Task, projectSlug?: string): string {
  const body = task.notes ? `${task.title}\n\n${task.notes}` : task.title;
  const proto = task.agent_protocol?.trim() || "";
  const parts = [body];
  if (proto) parts.push(proto);
  const marker = projectSlug
    ? `⟨celeborn:${projectSlug}/${task.id}⟩`
    : `⟨celeborn:${task.id}⟩`;
  parts.push(marker);
  return parts.join("\n\n");
}

/** Copy text to the clipboard. The async Clipboard API only exists in a secure context
 *  (https or localhost), so opening the board over a LAN IP (http://192.168.x.x:3000) leaves
 *  `navigator.clipboard` undefined. Fall back to a hidden-textarea + execCommand('copy'), which
 *  still works on plain-HTTP origins. Returns true on success. */
async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall through to the legacy path
    }
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

function relTime(iso: string): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

type EditFocus = "title" | "notes";

interface CardActions {
  onSelect: (id: string) => void;
  onCopy: (task: Task) => void;
  onCopyId: (task: Task) => void;
  onHandoff: (task: Task) => void;
  onReorder: (id: string, dir: "up" | "down") => void;
  onDone: (task: Task) => void;
  onMove: (id: string, state: TaskState) => void;
  onDelete: (task: Task) => void;
  onStartEdit: (task: Task, focus?: EditFocus) => void;
  onSaveEdit: () => void;
  onCancelEdit: () => void;
  onEditDraftChange: (draft: { title: string; notes: string }) => void;
}

function Card({
  task,
  selected,
  first,
  last,
  editing,
  editDraft,
  editFocus,
  justMoved,
  actions,
  tokens,
}: {
  task: Task;
  selected: boolean;
  first: boolean;
  last: boolean;
  editing: boolean;
  editDraft: { title: string; notes: string };
  editFocus: EditFocus;
  /** True the first render this card lands in a new column (or is brand-new) — triggers the fade-out glow once. */
  justMoved: boolean;
  actions: CardActions;
  /** Live context window (tokens) of the session working this card, if any — drives the /clear-nudge band. */
  tokens?: number;
}) {
  const titleRef = useRef<HTMLInputElement>(null);
  const notesRef = useRef<HTMLTextAreaElement>(null);
  const cardRef = useRef<HTMLElement>(null);
  // Done cards are the lowest-priority content on the board (CELE-t110): they collapse to just the
  // first 128 chars of their title and hide the rest (full title, notes, stop, tags) behind "Show
  // more". Most of the time you only want to scan the titles of what's finished.
  const [expanded, setExpanded] = useState(false);
  const DONE_TITLE_CAP = 128;
  const isDone = task.state === "done";
  const titleTooLong = task.title.length > DONE_TITLE_CAP;
  const hasDetail = titleTooLong || !!task.notes;
  const shownTitle =
    isDone && !expanded && titleTooLong
      ? `${task.title.slice(0, DONE_TITLE_CAP).trimEnd()}…`
      : task.title;
  // For Done cards, only reveal notes / stop / footer once expanded; other columns always show them.
  const showDetail = !isDone || expanded;

  useEffect(() => {
    if (!editing) return;
    const el = editFocus === "notes" ? notesRef.current : titleRef.current;
    el?.focus();
    if (editFocus === "notes") notesRef.current?.setSelectionRange(notesRef.current.value.length, notesRef.current.value.length);
  }, [editing, editFocus]);

  // Drag-to-move (parity with the hosted board). Attached as a native DOM listener via ref rather than
  // an onDragStart prop, because motion.article treats onDragStart/onDrag as its own gesture callbacks
  // (they wouldn't reach the DOM). The drop target (Column) reads this id and moves the card.
  useEffect(() => {
    const el = cardRef.current;
    if (!el || editing) return;
    const onDragStart = (e: DragEvent) => {
      e.dataTransfer?.setData("text/celeborn-task", task.id);
      if (e.dataTransfer) e.dataTransfer.effectAllowed = "move";
      el.setAttribute("data-dragging", "true");
    };
    const onDragEnd = () => el.removeAttribute("data-dragging");
    el.addEventListener("dragstart", onDragStart);
    el.addEventListener("dragend", onDragEnd);
    return () => {
      el.removeEventListener("dragstart", onDragStart);
      el.removeEventListener("dragend", onDragEnd);
    };
  }, [task.id, editing]);

  return (
    <motion.article
      // No position/size animation — cards snap straight to their new slot so nothing else moves to
      // distract the eye. The only signal is the glow: a card that just moved animates its box-shadow
      // from a blue edge glow (GLOW_ON) to nothing (GLOW_OFF) over 5s. A card that didn't move keeps
      // GLOW_OFF with a zero-length transition, so a normal render / poll never lights up.
      initial={false}
      animate={{ boxShadow: justMoved ? [GLOW_ON, GLOW_OFF] : GLOW_OFF }}
      transition={{ boxShadow: justMoved ? GLOW_TRANSITION : { duration: 0 } }}
      ref={cardRef}
      className="card"
      data-state={task.state}
      data-selected={selected || undefined}
      data-editing={editing || undefined}
      data-draggable={!editing || undefined}
      draggable={!editing}
      onClick={() => actions.onSelect(task.id)}
    >
      <header className="card-head">
        <span
          className="card-id"
          role="button"
          tabIndex={0}
          aria-label="Click to copy the task ID"
          onClick={(e) => {
            e.stopPropagation();
            actions.onCopyId(task);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              e.stopPropagation();
              actions.onCopyId(task);
            }
          }}
        >
          {task.display_id ?? task.id}
        </span>
        {task.owner ? (
          <span
            className="card-owner"
            title={task.owner_model ? `${[task.owner_family, task.owner_model].filter(Boolean).join(" · ")}` : undefined}
          >
            @{task.owner}
            {task.owner_model ? <span className="card-owner-model"> · {task.owner_model}</span> : null}
          </span>
        ) : null}
        {/* Live /clear-nudge band (CELE-t131): the context window of the session working this card.
            Replaces the standalone active-agents widget — the nudge now rides the card itself. */}
        {tokens !== undefined && task.state === "doing" ? (
          (() => {
            const k = Math.round(tokens / 1000);
            const b = band(k);
            return (
              <span className="card-band" style={{ ["--band" as string]: b.color }} title={`${k}k context tokens — ${b.word}`}>
                {b.word} · {k}k
              </span>
            );
          })()
        ) : null}
      </header>
      {task.state === "doing" ? (
        <SandProgress
          seed={task.display_id}
          value={Math.max(0, Math.min(100, Math.round(task.progress ?? 0)))}
          status={(task.blocked_by.length > 0 ? "solving" : "moving") as SandStatus}
        />
      ) : null}
      {task.state === "doing" && task.subtasks && task.subtasks.length > 0 ? (
        <ul className="card-subtasks">
          {task.subtasks.map((s, i) => (
            <li key={i} className={s.done ? "done" : undefined}>
              <span className="box">{s.done ? "✓" : "○"}</span>
              <span className="txt">{s.text}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {editing ? (
        <div className="card-edit" onClick={(e) => e.stopPropagation()}>
          <input
            ref={titleRef}
            className="card-edit-title"
            value={editDraft.title}
            placeholder="Title"
            onChange={(e) => actions.onEditDraftChange({ ...editDraft, title: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                actions.onCancelEdit();
              } else if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                actions.onSaveEdit();
              }
            }}
          />
          <textarea
            ref={notesRef}
            className="card-edit-notes"
            value={editDraft.notes}
            placeholder="Notes (optional) — Shift+Enter for newline, ⌘Enter to save"
            rows={3}
            onChange={(e) => actions.onEditDraftChange({ ...editDraft, notes: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                actions.onCancelEdit();
              } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                actions.onSaveEdit();
              }
            }}
          />
          <div className="card-edit-actions">
            <button type="button" className="text-btn" onClick={() => actions.onSaveEdit()}>
              Save
            </button>
            <button type="button" className="text-btn" onClick={() => actions.onCancelEdit()}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <>
          <p
            className="card-title card-editable"
            onDoubleClick={(e) => {
              e.stopPropagation();
              actions.onStartEdit(task, "title");
            }}
          >
            {shownTitle}
          </p>
          {showDetail && task.notes ? (
            <p
              className="card-notes card-editable"
              onDoubleClick={(e) => {
                e.stopPropagation();
                actions.onStartEdit(task, "notes");
              }}
            >
              {task.notes}
            </p>
          ) : null}
          {isDone && hasDetail ? (
            <button
              type="button"
              className="card-show-more"
              onClick={(e) => {
                e.stopPropagation();
                setExpanded((v) => !v);
              }}
            >
              {expanded ? "Show less" : "Show more"}
            </button>
          ) : null}
        </>
      )}
      {task.stop && showDetail ? (
        <p className="card-stop" title="Stop condition — the clean place to /clear">
          🛑 {task.stop}
        </p>
      ) : null}
      {task.agent_protocol ? (
        <p className="agent-protocol" aria-hidden="true" data-agent-only>
          {task.agent_protocol}
        </p>
      ) : null}
      {showDetail && (task.tags.length > 0 || task.blocked_by.length > 0) ? (
        <footer className="card-foot">
          {task.tags.map((t) => (
            <span key={t} className="tag">
              {t}
            </span>
          ))}
          {task.blocked_by.length > 0 ? (
            <span className="blocked-by" title={`Blocked by ${task.blocked_by.join(", ")}`}>
              ⛔ {task.blocked_by.join(", ")}
            </span>
          ) : null}
        </footer>
      ) : null}
      {!editing ? (
      <div className="card-actions" onClick={(e) => e.stopPropagation()}>
        <button
          className="icon-btn edit"
          title="Edit title and notes (double-click text)"
          onClick={() => actions.onStartEdit(task, "title")}
        >
          ✏️
        </button>
        <div className="reorder">
          <button
            className="icon-btn"
            title="Move up (higher priority)"
            disabled={first}
            onClick={() => actions.onReorder(task.id, "up")}
          >
            ▲
          </button>
          <button
            className="icon-btn"
            title="Move down (lower priority)"
            disabled={last}
            onClick={() => actions.onReorder(task.id, "down")}
          >
            ▼
          </button>
        </div>
        <div className="spacer" />
        {/* Mark complete — moves the card to Done, where it lands on top of the column (newest-done
            first). Done cards don't carry it (nothing left to complete). */}
        {task.state !== "done" ? (
          <button
            className="text-btn done"
            title="Mark done — move to the top of the Done column (d)"
            onClick={() => actions.onDone(task)}
          >
            ✓ Done
          </button>
        ) : null}
        <button
          className="text-btn"
          title="Copy prompt (with its card id) — paste into the model you want to work it (⌘C)"
          onClick={() => actions.onCopy(task)}
        >
          📋 Copy
        </button>
        {/* Handoff = re-notify the owner who already claimed this card. Before a card is claimed
            there is no target to push to (Celeborn can't know which models are live) — Copy + paste
            is how first contact happens. So Handoff only appears once the card has an owner. */}
        {task.state !== "done" && task.owner ? (
          <button
            className="text-btn handoff"
            title={`Re-send to @${task.owner} (the owner who claimed it) (Enter)`}
            onClick={() => actions.onHandoff(task)}
          >
            🏹 Handoff
          </button>
        ) : null}
        {/* Delete permanently removes the card from tasks.md — irreversible, so it confirms first.
            Every card carries it (keyboard: Shift+Backspace on the selected card). */}
        <button
          className="text-btn delete"
          title="Delete this card permanently (⇧⌫)"
          onClick={() => actions.onDelete(task)}
        >
          🗑 Delete
        </button>
      </div>
      ) : null}
    </motion.article>
  );
}

function Column({
  state,
  tasks,
  selectedId,
  editingId,
  editDraft,
  editFocus,
  movedIds,
  actions,
  adder,
  tokensByTask,
}: {
  state: TaskState;
  tasks: Task[];
  selectedId: string | null;
  editingId: string | null;
  editDraft: { title: string; notes: string };
  editFocus: EditFocus;
  movedIds: Set<string>;
  actions: CardActions;
  adder?: React.ReactNode;
  tokensByTask: Map<string, number>;
}) {
  const meta = STATE_META[state];
  const [over, setOver] = useState(false);
  return (
    <section
      className={`column${over ? " column-over" : ""}`}
      style={{ ["--accent" as string]: meta.accent }}
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        const id = e.dataTransfer.getData("text/celeborn-task");
        if (id) actions.onMove(id, state);
      }}
    >
      <header className="column-head">
        <span className="column-dot" />
        <h2>{meta.label}</h2>
        <span className="column-count">{tasks.length}</span>
      </header>
      <div className="column-body">
        {adder}
        {tasks.length === 0 && !adder ? (
          <p className="column-empty">—</p>
        ) : (
          tasks.map((t, i) => (
            <Card
              key={t.id}
              task={t}
              selected={t.id === selectedId}
              first={i === 0}
              last={i === tasks.length - 1}
              editing={editingId === t.id}
              editDraft={editDraft}
              editFocus={editFocus}
              justMoved={movedIds.has(t.id)}
              actions={actions}
              tokens={tokensByTask.get(t.id)}
            />
          ))
        )}
      </div>
    </section>
  );
}

export default function TaskKanban({
  initial,
  embedded = false,
  onBoardChange,
}: {
  initial: TaskBoard;
  /** When true, render only the column grid (shell provides header/tabs). */
  embedded?: boolean;
  /** Fired when a poll or mutation refreshes the board (for live header timestamps). */
  onBoardChange?: (board: TaskBoard) => void;
}) {
  const [board, setBoard] = useState<TaskBoard>(initial);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  // CELE-t109: the add form is two boxes — a concise title and a longer description (notes) — so
  // typed cards no longer dump everything into the title. `draft` is the title; `draftNote` the body.
  const [draftNote, setDraftNote] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState({ title: "", notes: "" });
  const [editFocus, setEditFocus] = useState<EditFocus>("title");
  // Gate time-relative rendering until after mount so the server-rendered HTML (which would compute a
  // slightly different relTime) doesn't trip Next.js's hydration mismatch check.
  const [mounted, setMounted] = useState(false);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => setMounted(true), []);

  const flash = useCallback((msg: string) => {
    setToast(msg);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 2400);
  }, []);

  const onPoll = useCallback(
    (next: TaskBoard) => {
      setBoard(next);
      onBoardChange?.(next);
    },
    [onBoardChange],
  );
  // Poll for CLI-driven changes (claim/ship/move). Pauses while add/edit inputs are open.
  useJsonPoll<TaskBoard>("/api/tasks", POLL_MS, adding || editingId !== null, onPoll);

  // Live context windows (CELE-t131): the same /api/agents feed the active-agents chips read, joined
  // onto cards by task_id so each DOING card carries its owning session's /clear-nudge band.
  const [agents, setAgents] = useState<ActiveAgentsData | null>(null);
  useJsonPoll<ActiveAgentsData>("/api/agents", POLL_MS, false, setAgents);
  const tokensByTask = useMemo(() => {
    const m = new Map<string, number>();
    for (const a of agents?.agents ?? []) {
      // Fullest window wins if two live sessions point at the same card (the nudge should be loudest).
      if (a.task_id && a.tokens > (m.get(a.task_id) ?? 0)) m.set(a.task_id, a.tokens);
    }
    return m;
  }, [agents]);

  const byState = useCallback(
    (s: TaskState) => sortByColumnRules(board.tasks, s),
    [board],
  );
  // Flat, column-major order — the spine for keyboard ↑/↓ selection across the whole board.
  const ordered = useMemo(() => board.states.flatMap((s) => byState(s)), [board, byState]);

  // Moved-card detection for the glow (t48): a card "moved" the first render its column differs from
  // the one it was in last (a move), or it's brand-new on the board (an add). We compare against the
  // PREVIOUS board's state-by-id, captured in a ref AFTER each render so this render sees the prior
  // snapshot. First render has no prior snapshot → empty set, so a fresh page load never glows.
  const prevStateRef = useRef<Record<string, TaskState> | null>(null);
  const movedIds = useMemo(() => {
    const prev = prevStateRef.current;
    const set = new Set<string>();
    if (prev) {
      for (const t of board.tasks) {
        if (prev[t.id] === undefined || prev[t.id] !== t.state) set.add(t.id);
      }
    }
    return set;
  }, [board]);
  useEffect(() => {
    const snap: Record<string, TaskState> = {};
    for (const t of board.tasks) snap[t.id] = t.state;
    prevStateRef.current = snap;
  }, [board]);

  const mutate = useCallback(
    async (payload: Record<string, unknown>) => {
      try {
        const res = await fetch("/api/tasks", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) {
          flash(data?.error || "Request failed");
          return false;
        }
        const next = data as TaskBoard;
        setBoard(next);
        onBoardChange?.(next);
        return true;
      } catch {
        flash("Network error");
        return false;
      }
    },
    [flash, onBoardChange],
  );

  const onCopy = useCallback(
    async (task: Task) => {
      if (await copyText(taskPrompt(task, board.project_slug))) {
        flash(`Copied [${task.id}] to clipboard`);
      } else {
        flash("Clipboard blocked by the browser");
      }
    },
    [flash, board.project_slug],
  );

  const onCopyId = useCallback(
    async (task: Task) => {
      const id = task.display_id ?? task.id;
      if (await copyText(id)) {
        flash(`Copied ${id}`);
      } else {
        flash("Clipboard blocked by the browser");
      }
    },
    [flash],
  );

  const onHandoff = useCallback(
    async (task: Task) => {
      if (await mutate({ action: "handoff", id: task.id })) {
        flash(`🏹 Handed off [${task.id}] — it posts on the model's next turn`);
      }
    },
    [mutate, flash],
  );

  const onReorder = useCallback(
    (id: string, dir: "up" | "down") => mutate({ action: "reorder", id, dir }),
    [mutate],
  );

  const onDone = useCallback(
    async (task: Task) => {
      if (await mutate({ action: "move", id: task.id, state: "done" })) {
        flash(`✓ Completed [${task.id}] → top of Done`);
      }
    },
    [mutate, flash],
  );

  // Drag-to-move: drop a card onto a column to set its state (parity with the hosted board).
  const onMove = useCallback(
    async (id: string, state: TaskState) => {
      const task = board.tasks.find((t) => t.id === id);
      if (!task || task.state === state) return;
      if (await mutate({ action: "move", id, state })) {
        flash(`Moved [${id}] → ${STATE_META[state].label}`);
      }
    },
    [mutate, flash, board],
  );

  const onStartEdit = useCallback((task: Task, focus: EditFocus = "title") => {
    setEditingId(task.id);
    setEditDraft({ title: task.title, notes: task.notes });
    setEditFocus(focus);
    setSelectedId(task.id);
  }, []);

  const onCancelEdit = useCallback(() => {
    setEditingId(null);
    setEditDraft({ title: "", notes: "" });
  }, []);

  const onSaveEdit = useCallback(async () => {
    if (!editingId) return;
    const title = editDraft.title.trim();
    if (!title) {
      flash("Title required");
      return;
    }
    const task = board.tasks.find((t) => t.id === editingId);
    const note = editDraft.notes;
    const payload: Record<string, unknown> = { action: "edit", id: editingId, title, note };
    if (task && task.title === title && task.notes === note) {
      onCancelEdit();
      return;
    }
    if (await mutate(payload)) {
      onCancelEdit();
      flash(`Saved [${editingId}]`);
    }
  }, [editingId, editDraft, board.tasks, mutate, flash, onCancelEdit]);

  const onEditDraftChange = useCallback((next: { title: string; notes: string }) => {
    setEditDraft(next);
  }, []);

  const onDelete = useCallback(
    async (task: Task) => {
      // Deletion is irreversible (the card leaves tasks.md), so confirm before firing.
      if (!window.confirm(`Delete [${task.id}] "${task.title}"? This can't be undone.`)) return;
      if (await mutate({ action: "delete", id: task.id })) {
        if (selectedId === task.id) setSelectedId(null);
        flash(`🗑 Deleted [${task.id}]`);
      }
    },
    [mutate, flash, selectedId],
  );

  const submitAdd = useCallback(async () => {
    const title = draft.trim();
    const note = draftNote.trim();
    if (!title) {
      // No title → nothing to add. Drop an empty form silently; keep it open if a description was typed.
      if (!note) setAdding(false);
      return;
    }
    if (await mutate({ action: "add", title, note })) {
      setDraft("");
      setDraftNote("");
      flash("Added card to To Do");
    }
  }, [draft, draftNote, mutate, flash]);

  const actions: CardActions = useMemo(
    () => ({
      onSelect: setSelectedId,
      onCopy,
      onCopyId,
      onHandoff,
      onReorder,
      onDone,
      onMove,
      onDelete,
      onStartEdit,
      onSaveEdit,
      onCancelEdit,
      onEditDraftChange,
    }),
    [onCopy, onCopyId, onHandoff, onReorder, onDone, onMove, onDelete, onStartEdit, onSaveEdit, onCancelEdit, onEditDraftChange],
  );

  // Keyboard: ↑/↓ select across the board · Enter/h handoff · c copy · Shift+↑/↓ reprioritize.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = document.activeElement;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) return;
      if (!ordered.length) return;
      const idx = ordered.findIndex((t) => t.id === selectedId);
      const selected = idx >= 0 ? ordered[idx] : null;

      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        const dir = e.key === "ArrowDown" ? 1 : -1;
        if (e.shiftKey && selected) {
          e.preventDefault();
          onReorder(selected.id, dir === 1 ? "down" : "up");
          return;
        }
        e.preventDefault();
        const nextIdx = idx < 0 ? 0 : Math.min(ordered.length - 1, Math.max(0, idx + dir));
        setSelectedId(ordered[nextIdx].id);
      } else if ((e.key === "Backspace" || e.key === "Delete") && e.shiftKey && selected) {
        e.preventDefault();
        onDelete(selected);
      } else if (e.key === "d" && selected && selected.state !== "done") {
        e.preventDefault();
        onDone(selected);
      } else if ((e.key === "Enter" || e.key === "h") && selected && selected.state !== "done") {
        e.preventDefault();
        onHandoff(selected);
      } else if (e.key.toLowerCase() === "c" && (e.metaKey || e.ctrlKey) && selected) {
        // ⌘C / Ctrl+C copies the selected card — but only when the user isn't copying real
        // highlighted text, so we never hijack a genuine text selection.
        if (!window.getSelection()?.toString()) {
          e.preventDefault();
          onCopy(selected);
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ordered, selectedId, onHandoff, onCopy, onReorder, onDone, onDelete]);

  const grid = board.missing ? (
        <div className="empty-hint">
          <p>
            No <code>tasks.json</code> found.
          </p>
          <p>
            Create one from the repo root: <code>celeborn tasks add &quot;your first task&quot;</code>
          </p>
          <p className="muted">Looked in: {board.source}</p>
        </div>
      ) : (
        // reducedMotion="user" — honor the OS "reduce motion" setting: the glow snaps on and clears
        // without a tween (no motion, just colour).
        <MotionConfig reducedMotion="user">
        <div className="columns">
          {/* Filter to states we have render metadata for. tasks.json is derived data the CLI owns;
              right after an upgrade it can still list a retired column (e.g. "blocked" before
              CELE-t135) until the next `celeborn tasks` run regenerates it — skip those rather than
              dereference a missing STATE_META entry and crash the whole board. */}
          {board.states.filter((s) => STATE_META[s]).map((s) => {
            const column = (
            <Column
              key={s}
              state={s}
              tasks={byState(s)}
              selectedId={selectedId}
              editingId={editingId}
              editDraft={editDraft}
              editFocus={editFocus}
              movedIds={movedIds}
              actions={actions}
              tokensByTask={tokensByTask}
              adder={
                s === "todo" ? (
                  <div className="adder">
                    {adding ? (
                      <div className="adder-form">
                        <input
                          className="adder-input adder-title"
                          autoFocus
                          placeholder="Title — a short summary"
                          value={draft}
                          onChange={(e) => setDraft(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              e.preventDefault();
                              submitAdd();
                            } else if (e.key === "Escape") {
                              setAdding(false);
                              setDraft("");
                              setDraftNote("");
                            }
                          }}
                        />
                        <textarea
                          className="adder-input adder-desc"
                          placeholder="Description (optional) — Shift+Enter for newline, ⌘Enter to add"
                          value={draftNote}
                          onChange={(e) => setDraftNote(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                              e.preventDefault();
                              submitAdd();
                            } else if (e.key === "Escape") {
                              setAdding(false);
                              setDraft("");
                              setDraftNote("");
                            }
                          }}
                        />
                        <div className="adder-actions">
                          <button className="adder-save" onClick={submitAdd} disabled={!draft.trim()}>
                            Add card
                          </button>
                          <button
                            className="adder-cancel"
                            onClick={() => {
                              setAdding(false);
                              setDraft("");
                              setDraftNote("");
                            }}
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    ) : (
                      <button className="add-btn" onClick={() => setAdding(true)}>
                        + Add card
                      </button>
                    )}
                  </div>
                ) : undefined
              }
            />
            );
            // CELE-t131 part D: the standalone active-agents panel is retired — the /clear-nudge now
            // rides each DOING/Blocked card's own band pill (see `card-band` above), so every column is
            // a bare grid child.
            return column;
          })}
        </div>
        </MotionConfig>
      );

  if (embedded) {
    return (
      <>
        {grid}
        {toast ? <div className="toast">{toast}</div> : null}
      </>
    );
  }

  const total = board.tasks.length;

  return (
    <main className="board-page">
      <header className="board-head">
        <div className="brand">
          <span className="bow">🏹</span>
          <div>
            <h1>Celeborn</h1>
            <p className="subtitle">
              Task board · {total} task{total === 1 ? "" : "s"}
            </p>
          </div>
        </div>
        <div className="board-meta">
          <span className="kbd-legend">
            <kbd>↑</kbd>
            <kbd>↓</kbd> select · <kbd>Enter</kbd> handoff · <kbd>⌘C</kbd> copy · <kbd>⇧↑↓</kbd> reorder ·{" "}
            <kbd>d</kbd> done · <kbd>⇧⌫</kbd> delete
          </span>
          <span className="live-dot" /> live
          {mounted && board.generated_at ? <span className="updated">· updated {relTime(board.generated_at)}</span> : null}
        </div>
      </header>
      {grid}
      {toast ? <div className="toast">{toast}</div> : null}
    </main>
  );
}
