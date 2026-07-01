import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { promisify } from "node:util";
import { tasksJsonPath } from "@/lib/tasks";

const run = promisify(execFile);

/**
 * Resolve how to invoke the CLI. Prefer the bundled `scripts/celeborn.py` in the repo the board
 * points at (so lazily-imported modules like celeborn_jira resolve). Override with CELEBORN_BIN.
 */
function cliInvocation(): { bin: string; prefix: string[] } {
  if (process.env.CELEBORN_BIN) {
    return { bin: process.env.CELEBORN_BIN, prefix: [] };
  }
  const script = path.join(repoDir(), "scripts", "celeborn.py");
  if (existsSync(script)) {
    return { bin: process.env.PYTHON || "python3", prefix: [script] };
  }
  return { bin: "celeborn", prefix: [] };
}

/**
 * The repo dir to operate in: the parent of the `.context/` that holds tasks.json. Passed to the CLI
 * as `--path` so writes land in the same context the board reads, regardless of the board's own cwd.
 */
function repoDir(): string {
  return path.dirname(path.dirname(tasksJsonPath()));
}

/**
 * Invoke `celeborn --path <repo> <args…>`. Throws on a non-zero exit so the route can surface it.
 * Args are passed as an array (no shell), so task titles/notes can't inject.
 */
export async function celeborn(
  args: string[],
  opts?: { env?: NodeJS.ProcessEnv; timeoutMs?: number },
): Promise<string> {
  const { bin, prefix } = cliInvocation();
  const { stdout } = await run(bin, [...prefix, "--path", repoDir(), ...args], {
    timeout: opts?.timeoutMs ?? 10_000,
    maxBuffer: 1 << 20,
    env: opts?.env ? { ...process.env, ...opts.env } : process.env,
  });
  return stdout;
}

/** Run celeborn and parse the last JSON object printed to stdout (for `--json` subcommands). */
export async function celebornJson<T extends Record<string, unknown>>(
  args: string[],
  opts?: { env?: NodeJS.ProcessEnv; timeoutMs?: number },
): Promise<T> {
  const stdout = await celeborn(args, opts);
  const trimmed = stdout.trim();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    return JSON.parse(trimmed) as T;
  }
  const start = trimmed.indexOf("{");
  if (start < 0) {
    throw new Error(`celeborn did not return JSON: ${trimmed.slice(0, 200)}`);
  }
  return JSON.parse(trimmed.slice(start)) as T;
}
