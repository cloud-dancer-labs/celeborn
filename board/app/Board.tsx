"use client";

import { useEffect, useMemo, useState } from "react";
import type { TaskBoard as TaskBoardData } from "@/lib/tasks";
import BoardHeader from "./BoardHeader";
import JiraPanel from "./JiraPanel";
import RunDashboard from "./RunDashboard";
import SavingsBar from "./SavingsBar";
import TaskKanban from "./TaskBoard";

type View = "tasks" | "run";

const SUBTITLE: Record<View, (t: TaskBoardData) => string> = {
  tasks: (t) => `Task board · ${t.tasks.length} task${t.tasks.length === 1 ? "" : "s"}`,
  run: () => "Run dashboard · live swarm of Elves",
};

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

export default function Board({
  initialTasks,
  initialView = "tasks",
}: {
  initialTasks: TaskBoardData;
  initialView?: View;
}) {
  const [view, setView] = useState<View>(initialView);
  const [mounted, setMounted] = useState(false);
  const [tasksAt, setTasksAt] = useState(initialTasks.generated_at);
  const [board, setBoard] = useState(initialTasks);

  useEffect(() => setMounted(true), []);

  // Gmail-style live tab title: prefix the To Do backlog count so a board left open in a tab shows
  // the count at a glance. Rendered below as a React-managed <title> (React 19 hoists it to <head>),
  // NOT set imperatively — an imperative document.title gets clobbered by the framework's own metadata
  // commit after hydration. As a rendered element it stays correct across re-renders and updates
  // automatically as the board polls; it's also computed during SSR so the first paint carries it.
  // "(0)" is dropped so an empty backlog reads clean. Owns the <title> outright — layout.tsx no longer
  // sets one, so there is exactly one title element.
  const tabTitle = useMemo(() => {
    const todo = board.tasks.reduce((n, t) => (t.state === "todo" ? n + 1 : n), 0);
    const name = board.project_name || board.project_slug || "";
    const base = name ? `🏹 - ${name}` : "🏹";
    return todo > 0 ? `(${todo}) ${base}` : base;
  }, [board]);

  const generatedAt = tasksAt;
  const projectName = initialTasks.project_name || initialTasks.project_slug || "";

  return (
    <main className="board-page">
      <title>{tabTitle}</title>
      <BoardHeader
        active={view}
        subtitle={SUBTITLE[view](initialTasks)}
        projectName={projectName || undefined}
        onSelect={setView}
        meta={
          <>
            <JiraPanel />
            <span className="live-dot" /> live
            {mounted && generatedAt ? (
              <span className="updated">· updated {relTime(generatedAt)}</span>
            ) : null}
          </>
        }
        secondRow={
          view === "tasks" ? (
            <div className="board-meta-row">
              <span className="kbd-legend">
                <kbd>drag</kbd> to move · <kbd>double-click</kbd> a card to edit
              </span>
              <span className="kbd-legend">
                <kbd>↑</kbd>
                <kbd>↓</kbd> select · <kbd>Enter</kbd> handoff · <kbd>⌘C</kbd> copy · <kbd>d</kbd> done
              </span>
            </div>
          ) : null
        }
      />

      <SavingsBar />

      {view === "tasks" ? (
        <TaskKanban
          initial={initialTasks}
          embedded
          onBoardChange={(b) => { setTasksAt(b.generated_at); setBoard(b); }}
        />
      ) : (
        <RunDashboard />
      )}

      <LegalFooter />
    </main>
  );
}

// The board's required-agreement links, the way a standard web app footers them. The documents are
// published on the thot.ai site (apex domain); link out absolutely so they resolve from both the local
// board (localhost:3141) and the hosted board (celeborn.thot.ai).
function LegalFooter() {
  const year = new Date().getFullYear();
  return (
    <footer className="board-legal">
      <span className="board-legal-copy">
        © {year} Thot Technologies, LLC · Celeborn authored by Cloud Dancer
      </span>
      <nav className="board-legal-links" aria-label="Legal">
        <a href="https://thot.ai/privacy" target="_blank" rel="noopener noreferrer">Privacy</a>
        <a href="https://thot.ai/cookies" target="_blank" rel="noopener noreferrer">Cookies</a>
        <a href="https://thot.ai/user-agreement" target="_blank" rel="noopener noreferrer">User Agreement</a>
      </nav>
    </footer>
  );
}