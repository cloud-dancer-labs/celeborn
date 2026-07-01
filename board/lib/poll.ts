import { useEffect, useRef } from "react";

/** Poll a JSON endpoint; skip ticks while `paused`. Uses generated_at to avoid redundant renders. */
export function useJsonPoll<T extends { generated_at?: string }>(
  url: string,
  intervalMs: number,
  paused: boolean,
  onData: (data: T) => void,
) {
  const lastAt = useRef("");

  useEffect(() => {
    let alive = true;

    const tick = async () => {
      if (paused) return;
      try {
        const res = await fetch(url, { cache: "no-store" });
        if (!res.ok) return;
        const next = (await res.json()) as T;
        const stamp = next.generated_at ?? JSON.stringify(next);
        if (stamp === lastAt.current) return;
        lastAt.current = stamp;
        if (alive) onData(next);
      } catch {
        /* keep last good snapshot */
      }
    };

    tick();
    const id = setInterval(tick, intervalMs);
    const onVisible = () => {
      if (document.visibilityState === "visible") tick();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [url, intervalMs, paused, onData]);
}