import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  RotateCcw,
  Trash2,
  X
} from "lucide-react";

import {
  getActiveBrowseDeletion,
  getBrowseDeletion,
  undoBrowseDeletion
} from "./api";
import type { BrowseDeletion } from "./types";

const ACTIVE_STATUSES = new Set(["scheduled", "queued", "running"]);

export function useBrowseDeletionController(
  token: string,
  onFilesystemChanged: () => void
) {
  const [deletion, setDeletion] = useState<BrowseDeletion | null>(null);
  const [error, setError] = useState("");
  const [undoing, setUndoing] = useState(false);
  const notifiedRef = useRef(new Set<string>());

  useEffect(() => {
    let cancelled = false;
    if (!token) {
      setDeletion(null);
      setError("");
      return;
    }
    getActiveBrowseDeletion(token)
      .then(({ deletion: active }) => {
        if (!cancelled) setDeletion(active);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Deletion status is unavailable.");
        }
      });
    return () => { cancelled = true; };
  }, [token]);

  useEffect(() => {
    if (!deletion || !ACTIVE_STATUSES.has(deletion.status)) return;
    let stopped = false;
    const poll = async () => {
      try {
        const next = await getBrowseDeletion(token, deletion.id);
        if (!stopped) {
          setDeletion(next);
          setError("");
        }
      } catch (err) {
        if (!stopped) {
          setError(err instanceof Error ? err.message : "Deletion status is unavailable.");
        }
      }
    };
    const timer = window.setInterval(() => void poll(), 400);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [token, deletion?.id, deletion?.status]);

  useEffect(() => {
    if (!deletion || !["completed", "failed"].includes(deletion.status)) return;
    if (!notifiedRef.current.has(deletion.id)) {
      notifiedRef.current.add(deletion.id);
      onFilesystemChanged();
    }
  }, [deletion, onFilesystemChanged]);

  useEffect(() => {
    if (!deletion || ACTIVE_STATUSES.has(deletion.status)) return;
    const timer = window.setTimeout(
      () => setDeletion((current) => current?.id === deletion.id ? null : current),
      deletion.status === "cancelled" ? 3000 : 7000
    );
    return () => window.clearTimeout(timer);
  }, [deletion?.id, deletion?.status]);

  const onScheduled = useCallback((next: BrowseDeletion) => {
    setError("");
    setDeletion(next);
  }, []);

  const undo = useCallback(async () => {
    if (!deletion) return;
    setUndoing(true);
    setError("");
    try {
      setDeletion(await undoBrowseDeletion(token, deletion.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "The deletion could not be undone.");
      try {
        setDeletion(await getBrowseDeletion(token, deletion.id));
      } catch {
        // Keep the last authoritative state when a follow-up read also fails.
      }
    } finally {
      setUndoing(false);
    }
  }, [token, deletion]);

  const pendingPaths = useMemo(() => new Set(
    deletion && ACTIVE_STATUSES.has(deletion.status)
      ? deletion.items.map((item) => item.path)
      : []
  ), [deletion]);

  return {
    deletion,
    error,
    undoing,
    pendingPaths,
    onScheduled,
    undo,
    dismiss: () => {
      setDeletion(null);
      setError("");
    }
  };
}

export function BrowseDeletionToast({
  deletion,
  error,
  undoing,
  onUndo,
  onDismiss
}: {
  deletion: BrowseDeletion | null;
  error: string;
  undoing: boolean;
  onUndo: () => void;
  onDismiss: () => void;
}) {
  const [now, setNow] = useState(Date.now());
  const [serverOffsetMs, setServerOffsetMs] = useState(0);
  useEffect(() => {
    if (!deletion) return;
    const clientNow = Date.now();
    const serverNow = new Date(deletion.server_time).getTime();
    setNow(clientNow);
    setServerOffsetMs(Number.isFinite(serverNow) ? serverNow - clientNow : 0);
  }, [deletion?.id, deletion?.server_time]);
  useEffect(() => {
    if (deletion?.status !== "scheduled") return;
    const timer = window.setInterval(() => setNow(Date.now()), 200);
    return () => window.clearInterval(timer);
  }, [deletion?.id, deletion?.status]);

  if (!deletion && !error) return null;
  const deadline = deletion ? new Date(deletion.delete_after).getTime() : 0;
  const remainingMs = Math.max(0, deadline - (now + serverOffsetMs));
  const remainingSeconds = Math.ceil(remainingMs / 1000);
  const totalWindowMs = deletion
    ? Math.max(1, deadline - new Date(deletion.created_at).getTime())
    : 1;
  const remainingPercent = Math.min(100, (remainingMs / totalWindowMs) * 100);
  const canUndo = Boolean(
    deletion?.status === "scheduled" && deletion.undo_available && remainingMs > 0
  );
  const copy = deletion ? deletionCopy(deletion, remainingSeconds) : null;

  return (
    <aside
      className={`browse-deletion-toast ${deletion?.status || "error"}`}
      role={deletion?.status === "failed" || error ? "alert" : "status"}
      aria-live="polite"
    >
      <div className="deletion-toast-icon">
        {deletion?.status === "completed" ? <CheckCircle2 />
          : deletion?.status === "failed" || error ? <AlertTriangle />
          : deletion?.status === "queued" || deletion?.status === "running" ? <Loader2 className="spin" />
          : <Trash2 />}
      </div>
      <div className="deletion-toast-copy">
        <strong>{copy?.title || "Deletion status unavailable"}</strong>
        <span>{error || copy?.message}</span>
        {deletion?.status === "scheduled" && (
          <div className="deletion-countdown" aria-hidden="true">
            <span style={{ width: `${remainingPercent}%` }} />
          </div>
        )}
      </div>
      {canUndo ? (
        <button className="deletion-undo" disabled={undoing} onClick={onUndo}>
          {undoing ? <Loader2 className="spin" /> : <RotateCcw />}
          {undoing ? "Undoing…" : "Undo"}
        </button>
      ) : deletion && !ACTIVE_STATUSES.has(deletion.status) ? (
        <button className="icon-button deletion-dismiss" onClick={onDismiss} aria-label="Dismiss deletion notice"><X /></button>
      ) : null}
    </aside>
  );
}

function deletionCopy(deletion: BrowseDeletion, seconds: number) {
  const count = `${deletion.selected_count} item${deletion.selected_count === 1 ? "" : "s"}`;
  if (deletion.status === "scheduled") {
    return seconds > 0
      ? { title: "Permanent deletion scheduled", message: `${count} will be deleted in ${seconds} second${seconds === 1 ? "" : "s"}.` }
      : { title: "Undo window closed", message: `${count} are waiting for deletion.` };
  }
  if (deletion.status === "queued") {
    return { title: "Deletion queued", message: `${count} are waiting behind another disk task.` };
  }
  if (deletion.status === "running") {
    return { title: "Deleting permanently", message: `Removing ${count}. This cannot be undone.` };
  }
  if (deletion.status === "cancelled") {
    return { title: "Deletion cancelled", message: `${count} were left unchanged.` };
  }
  if (deletion.status === "failed") {
    return { title: "Deletion failed", message: deletion.error || `${count} could not be deleted.` };
  }
  const deleted = deletion.items.filter((item) => item.status === "deleted").length;
  const failed = deletion.items.length - deleted;
  return failed
    ? { title: "Deletion finished with warnings", message: `${deleted} deleted; ${failed} skipped or failed.` }
    : { title: "Deletion complete", message: `${deleted} item${deleted === 1 ? "" : "s"} permanently deleted.` };
}
