import { useCallback, useEffect, useState } from "react";
import type { SleeveDetail, SleeveDetailErrorBody } from "@/lib/types";

/**
 * Lazily load one sleeve's read-only detail record.
 *
 * Deliberately *not* folded into the dashboard's polling loop. The record carries
 * per-sleeve history, so it is fetched once when a sleeve is opened and then left alone:
 * a drawer showing a recorded, timestamped valuation has nothing to gain from being
 * re-fetched every 30 seconds, and re-fetching it would put the cost back into every
 * operator's session.
 */

export type SleeveDetailState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; detail: SleeveDetail }
  /** `code` is the server's stable reason (`unknown-sleeve`, `invalid-sleeve-id`, …). */
  | { status: "error"; code: string; message: string };

/** Dev-only: resolve a sleeve from the committed offline fixtures instead of the server. */
async function fromFixtures(sleeveId: string): Promise<SleeveDetailState> {
  const { SLEEVE_DETAIL_FIXTURES } = await import("@/lib/fixtures");
  const detail = SLEEVE_DETAIL_FIXTURES[sleeveId];
  if (detail) return { status: "ready", detail };
  return {
    status: "error",
    code: "unknown-sleeve",
    message:
      "No sleeve-detail fixture exists for that stable identifier. Available: " +
      Object.keys(SLEEVE_DETAIL_FIXTURES).join(", "),
  };
}

async function fromServer(sleeveId: string): Promise<SleeveDetailState> {
  const resp = await fetch(`/api/sleeve?sleeve_id=${encodeURIComponent(sleeveId)}`);
  if (resp.ok) {
    return { status: "ready", detail: (await resp.json()) as SleeveDetail };
  }
  // The endpoint answers refusals with a stable code and a written sentence. Prefer
  // those over inventing a message from the status line, but never assume the body
  // parses: a proxy or a crash can return something else entirely.
  try {
    const body = (await resp.json()) as SleeveDetailErrorBody;
    if (body?.error?.message) {
      return { status: "error", code: body.error.code, message: body.error.message };
    }
  } catch {
    // fall through to the generic message below
  }
  return {
    status: "error",
    code: "request-failed",
    message: `The sleeve detail request failed (HTTP ${resp.status}).`,
  };
}

export function useSleeveDetail(
  sleeveId: string | null,
  { useFixtures = false }: { useFixtures?: boolean } = {}
): { state: SleeveDetailState; reload: () => void } {
  const [state, setState] = useState<SleeveDetailState>({ status: "idle" });
  const [attempt, setAttempt] = useState(0);

  const reload = useCallback(() => setAttempt((value) => value + 1), []);

  useEffect(() => {
    if (!sleeveId) {
      setState({ status: "idle" });
      return;
    }
    // Guards a late response from a sleeve the operator has already navigated away
    // from: without it, closing one sleeve and opening another can render the first
    // one's record under the second one's heading.
    let current = true;
    setState({ status: "loading" });

    void (async () => {
      try {
        const next = useFixtures ? await fromFixtures(sleeveId) : await fromServer(sleeveId);
        if (current) setState(next);
      } catch (exc) {
        if (current) {
          setState({
            status: "error",
            code: "request-failed",
            message: exc instanceof Error ? exc.message : String(exc),
          });
        }
      }
    })();

    return () => {
      current = false;
    };
  }, [sleeveId, useFixtures, attempt]);

  return { state, reload };
}
