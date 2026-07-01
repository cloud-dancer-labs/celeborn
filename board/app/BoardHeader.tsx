"use client";

import Link from "next/link";
import type { ReactNode } from "react";

export type HeaderView = "tasks" | "run" | "settings";

const TABS: { view: HeaderView; label: string; href: string }[] = [
  { view: "tasks", label: "📋 Tasks", href: "/" },
  { view: "run", label: "🐝 Run", href: "/?view=run" },
  { view: "settings", label: "⚙️ Settings", href: "/settings" },
];

/**
 * The one true board header — brand + view tabs — shared verbatim across every view (Tasks, Run,
 * Settings). It is presentational: the tab UI is identical everywhere, but each page wires the
 * controls to suit its context (the hosted board can pass its own `onSelect`/links without forking
 * the markup). The active tab is always the page the user is on, so there is always a way back.
 *
 * - Tasks/Run toggle client-side when `onSelect` is provided (the live board page); otherwise they
 *   are links back to the board (e.g. from Settings, "🐝 Run" → `/?view=run`).
 * - Settings is always a route; when it's the active view it renders inert (no navigation to self).
 * - `meta` (Jira / live status) and `secondRow` (keyboard legend) are optional slots — board-only
 *   chrome that Settings simply omits.
 */
export default function BoardHeader({
  active,
  subtitle,
  projectName,
  onSelect,
  meta,
  secondRow,
}: {
  active: HeaderView;
  subtitle: string;
  projectName?: string;
  onSelect?: (view: "tasks" | "run") => void;
  meta?: ReactNode;
  secondRow?: ReactNode;
}) {
  return (
    <header className="board-head">
      <div className="brand">
        <span className="bow">🏹</span>
        <div>
          <h1>Celeborn</h1>
          <p className="subtitle">{subtitle}</p>
          {projectName ? <p className="board-project">{projectName}</p> : null}
        </div>
      </div>
      {/*
        The nav lives in its own absolutely-positioned, left-justified block, anchored to one
        fixed spot in the header — so Tasks / Run / Settings tabs sit in the SAME place on every
        view. The keyboard legend (`secondRow`, Tasks only) stacks directly beneath the tabs in
        this same left-aligned block, so the tabs always read as "left-justified over drag" by
        construction; on views without a legend the tabs simply stay put.
      */}
      <div className="board-nav">
        <nav className="view-tabs" aria-label="Board view">
          {TABS.map((tab) => {
            const isActive = active === tab.view;
            const common = {
              className: "view-tab",
              "data-active": isActive || undefined,
              "aria-current": isActive ? ("page" as const) : undefined,
            };
            // The active tab is inert — it's the current page, so clicking it goes nowhere.
            if (isActive) {
              return (
                <span key={tab.view} {...common} aria-label={tab.label}>
                  {tab.label}
                </span>
              );
            }
            // Tasks/Run toggle the live board client-side when this page owns that state.
            if (onSelect && tab.view !== "settings") {
              const toggleTo = tab.view;
              return (
                <button
                  key={tab.view}
                  type="button"
                  {...common}
                  onClick={() => onSelect(toggleTo)}
                >
                  {tab.label}
                </button>
              );
            }
            // Otherwise navigate (Settings, or returning to the board from another route).
            return (
              <Link key={tab.view} href={tab.href} {...common} prefetch={false}>
                {tab.label}
              </Link>
            );
          })}
        </nav>
        {/* Page-specific chrome under the tabs (e.g. the Tasks keyboard legend). */}
        {secondRow}
      </div>
      {/* Live status / Jira stays pinned top-right, on its own band above the legend. */}
      {meta ? (
        <div className="board-meta">
          <div className="board-meta-row">{meta}</div>
        </div>
      ) : null}
    </header>
  );
}
