import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, HardDrive, Loader2, RefreshCw, ShieldAlert, Trash2 } from "lucide-react";

import {
  cancelBrowseScan, createBrowseScan, getActiveDockerAction, getBrowseResult,
  getBrowseScan, getDockerAction, getDockerItems, getDockerUsage, getRecentDockerActions,
  getStorageSummary, previewDockerCleanup, scheduleDockerCleanup, undoDockerAction
} from "./api";
import type {
  BrowseResult, BrowseScan, DockerAction, DockerItems, DockerKind,
  DockerOutcome, DockerPreview, DockerUsage, StorageSummary
} from "./types";

const KINDS: DockerKind[] = ["images", "containers", "volumes", "cache"];
const ACTIVE = new Set(["scheduled", "queued", "running"]);

export function StorageDashboard({ token, onOpenBrowse }: { token: string; onOpenBrowse: (path: string) => void }) {
  const [capacity, setCapacity] = useState<StorageSummary | null>(null);
  const [docker, setDocker] = useState<DockerUsage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [kind, setKind] = useState<DockerKind>("images");
  const [listing, setListing] = useState<DockerItems | null>(null);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [allEligible, setAllEligible] = useState(false);
  const [ageDays, setAgeDays] = useState(30);
  const [preview, setPreview] = useState<DockerPreview | null>(null);
  const [phrase, setPhrase] = useState("");
  const [action, setAction] = useState<DockerAction | null>(null);
  const [recent, setRecent] = useState<DockerAction[]>([]);
  const [scan, setScan] = useState<BrowseScan | null>(null);
  const [scanResult, setScanResult] = useState<BrowseResult | null>(null);
  const [now, setNow] = useState(Date.now());
  const [serverOffsetMs, setServerOffsetMs] = useState(0);

  const refresh = useCallback(async (force = false) => {
    setLoading(true);
    setError("");
    try {
      const [nextCapacity, nextDocker] = await Promise.allSettled([
        getStorageSummary(token).then((value) => { setCapacity(value); return value; }),
        getDockerUsage(token, force).then((value) => { setDocker(value); return value; })
      ]);
      const failure = [nextCapacity, nextDocker].find((result) => result.status === "rejected");
      if (failure?.status === "rejected") setError(failure.reason instanceof Error ? failure.reason.message : "Storage information is unavailable.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Storage information is unavailable.");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    getActiveDockerAction(token).then(({ action: active }) => setAction(active)).catch(() => undefined);
    getRecentDockerActions(token).then(({ actions }) => setRecent(actions)).catch(() => undefined);
  }, [token]);

  useEffect(() => {
    if (!docker?.available || kind === "cache") { setListing(null); return; }
    let cancelled = false;
    getDockerItems(token, kind, page, search)
      .then((next) => { if (!cancelled) setListing(next); })
      .catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : "Docker items are unavailable."); });
    return () => { cancelled = true; };
  }, [token, docker?.available, kind, page, search, docker?.measured_at]);

  useEffect(() => {
    if (!scan || !["queued", "running"].includes(scan.status)) return;
    let stopped = false;
    const poll = async () => {
      try {
        const next = await getBrowseScan(token, scan.id);
        if (stopped) return;
        setScan(next);
        if (next.status === "completed") setScanResult(await getBrowseResult(token, scan.id));
      } catch (err) {
        if (!stopped) setError(err instanceof Error ? err.message : "Filesystem scan failed.");
      }
    };
    const timer = window.setInterval(() => void poll(), 800);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [token, scan?.id, scan?.status]);

  useEffect(() => {
    if (!action || !ACTIVE.has(action.status)) return;
    let stopped = false;
    const timer = window.setInterval(async () => {
      setNow(Date.now());
      try {
        const next = await getDockerAction(token, action.id);
        if (!stopped) {
          setAction(next);
          if (!ACTIVE.has(next.status)) {
            setSelected(new Set()); setAllEligible(false); setPreview(null);
            void refresh(true);
            getRecentDockerActions(token).then(({ actions }) => setRecent(actions)).catch(() => undefined);
          }
        }
      } catch (err) {
        if (!stopped) setError(err instanceof Error ? err.message : "Docker action status is unavailable.");
      }
    }, 700);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [token, action?.id, action?.status, refresh]);

  useEffect(() => {
    if (!action) return;
    const server = new Date(action.server_time).getTime();
    if (Number.isFinite(server)) setServerOffsetMs(server - Date.now());
  }, [action?.server_time]);

  async function startScan() {
    setError(""); setScanResult(null);
    try {
      const created = await createBrowseScan(token, capacity?.scan_root || "/");
      setScan(created.scan);
      if (created.scan.status === "completed") setScanResult(await getBrowseResult(token, created.scan.id));
    }
    catch (err) { setError(err instanceof Error ? err.message : "Could not start filesystem scan."); }
  }

  async function makePreview() {
    setError(""); setPreview(null); setPhrase("");
    try {
      setPreview(await previewDockerCleanup(token, kind, Array.from(selected), ageDays, allEligible));
    } catch (err) { setError(err instanceof Error ? err.message : "Could not preview Docker cleanup."); }
  }

  async function confirmCleanup() {
    if (!preview || phrase !== preview.confirmation_phrase) return;
    setError("");
    try { setAction(await scheduleDockerCleanup(token, preview.id, phrase)); setPreview(null); setPhrase(""); }
    catch (err) { setError(err instanceof Error ? err.message : "Docker cleanup was not scheduled."); }
  }

  async function undo() {
    if (!action) return;
    try {
      setAction(await undoDockerAction(token, action.id));
      getRecentDockerActions(token).then(({ actions }) => setRecent(actions)).catch(() => undefined);
    }
    catch (err) { setError(err instanceof Error ? err.message : "The cancellation window has closed."); }
  }

  function changeKind(next: DockerKind) {
    setKind(next); setPage(1); setSearch(""); setListing(null); setSelected(new Set());
    setAllEligible(false); setPreview(null); setPhrase("");
  }

  function toggle(id: string, checked: boolean) {
    setAllEligible(false);
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(id); else next.delete(id);
      return next;
    });
  }

  const usedPercent = capacity?.total_bytes ? Math.round(capacity.used_bytes / capacity.total_bytes * 100) : 0;
  const measured = scanResult?.allocated_bytes || 0;
  const gap = capacity && scanResult && capacity.used_bytes >= measured ? capacity.used_bytes - measured : null;
  const rootRows = useMemo(() => (scanResult?.entries || [])
    .filter((entry) => entry.kind === "directory")
    .sort((a, b) => (b.allocated_bytes || 0) - (a.allocated_bytes || 0))
    .slice(0, 15), [scanResult]);
  const countdown = action?.status === "scheduled"
    ? Math.max(0, Math.ceil((new Date(action.delete_after).getTime() - (now + serverOffsetMs)) / 1000))
    : 0;

  return <section className="storage-dashboard">
    <header className="page-header">
      <div><span className="eyebrow">Whole-disk view</span><h2>Storage</h2>
        <p>See what uses this computer’s disk, including Docker data that folder browsing cannot measure.</p></div>
      <button disabled={loading} onClick={() => void refresh(true)}><RefreshCw />{loading ? "Refreshing…" : "Refresh"}</button>
    </header>
    {error && <div className="alert" role="alert">{error}</div>}
    <section className="panel storage-overview">
      <h3><HardDrive /> Primary filesystem</h3>
      {capacity ? <><strong>{bytes(capacity.used_bytes)} of {bytes(capacity.total_bytes)} used ({usedPercent}%)</strong>
        <div className="storage-meter" role="progressbar" aria-label="Filesystem used" aria-valuemin={0} aria-valuemax={100} aria-valuenow={usedPercent}>
          <span style={{ width: `${Math.min(100, usedPercent)}%` }} /></div>
        <p>{bytes(capacity.available_bytes)} available. This is filesystem capacity, not a sum of Browse rows.</p></>
        : <p>{loading ? "Reading capacity…" : "Capacity unavailable."}</p>}
    </section>
    <section className="panel storage-overview">
      <div className="storage-section-header"><div><h3>Where files are measured</h3><p>Scan folders under {capacity?.scan_root || "the home filesystem"}. Root-owned paths may remain partial.</p></div>
        <button disabled={scan?.status === "queued" || scan?.status === "running"} onClick={() => void startScan()}>Scan filesystem</button></div>
      {scan && ["queued", "running"].includes(scan.status) && <div className="storage-scan-progress" role="status"><Loader2 className="spin" /> {scan.progress_message}
        <button onClick={() => void cancelBrowseScan(token, scan.id).then(setScan).catch((err) => setError(err instanceof Error ? err.message : "Could not cancel scan."))}>Cancel</button></div>}
      {scan?.status === "failed" && <div className="alert">{scan.error || "Scan failed."}</div>}
      {scanResult && <><p>Measured {bytes(measured)} in {scanResult.current_path} · {scanResult.error_count} scan errors · {gap !== null ? `${bytes(gap)} not accounted for by this scan` : "scan and filesystem totals are not directly comparable"}.</p>
        {scanResult.error_count > 0 && <p className="storage-note"><AlertTriangle />Permission failures make this a partial total.</p>}
        <p>Largest top-level folders. Files at this level are included in the measured total.</p>
        <div className="storage-folder-list">{rootRows.map((row) => <button key={row.path} onClick={() => onOpenBrowse(row.path)}>
          <span>{row.name}{row.size_state !== "complete" && <small> · {row.size_state}</small>}</span><strong>{bytes(row.allocated_bytes || 0)}</strong>
        </button>)}</div><button onClick={() => onOpenBrowse(scanResult.current_path)}>Open all entries in Browse</button></>}
    </section>
    <section className="panel storage-overview">
      <div className="storage-section-header"><div><h3>Docker storage</h3><p>Docker usage can overlap shared images, cache, and folder totals. Reclaimable amounts are estimates.</p></div></div>
      {!docker ? <p>{loading ? "Reading Docker usage…" : "Docker information unavailable."}</p>
        : !docker.available ? <p>{docker.message || "A local Docker daemon is unavailable."}</p>
          : <><p className="storage-note">{docker.same_filesystem
            ? "Docker data is on this filesystem. These numbers explain some space that an unprivileged folder scan may miss."
            : "Docker’s storage is separate or cannot be matched to this host filesystem; do not add these numbers to host usage."}</p>
            {docker.root_path && <p>Docker daemon data directory: <code>{docker.root_path}</code>{!docker.same_filesystem && " (this path may be inside Docker Desktop’s VM)."}</p>}
            <div className="docker-cards">{KINDS.map((entry) => {
              const group = docker.groups[entry];
              return <button key={entry} className={kind === entry ? "active" : ""} onClick={() => changeKind(entry)}>
                <strong>{entry === "cache" ? "Build cache" : entry[0].toUpperCase() + entry.slice(1)}</strong>
                <span>{group ? bytes(group.size_bytes) : "—"} used · {group?.count ?? 0} items</span>
                <small>{group ? bytes(group.reclaimable_bytes) : "—"} reclaimable estimate{entry === "cache" && " across all ages"}</small>
              </button>;
            })}</div>
            {kind === "cache" ? <div className="docker-cache-controls"><label>Unused cache last used over <select value={ageDays} onChange={(event) => { setAgeDays(Number(event.target.value)); setPreview(null); setPhrase(""); }}>
              <option value={7}>7 days ago</option><option value={30}>30 days ago</option><option value={90}>90 days ago</option>
            </select></label><p>The category estimate excludes image-shared cache and covers all ages. Newer cache can retain older parents.
              The preview checks those dependencies; Docker has no true dry-run and decides what it can remove at execution.</p></div>
              : <><div className="docker-list-toolbar"><input aria-label={`Search Docker ${kind}`} placeholder={`Search ${kind}`} value={search} onChange={(event) => { setSearch(event.target.value); setPage(1); setListing(null); }} />
                {kind !== "volumes" && <button onClick={() => { setAllEligible(true); setSelected(new Set()); }}>Select all eligible</button>}
                <span>{allEligible ? "All eligible selected" : `${selected.size} selected`}</span></div>
                <div className="docker-item-list">{listing?.items.map((item) => <label key={item.id} className={!item.eligible ? "docker-item blocked" : "docker-item"}>
                  <input type="checkbox" disabled={!item.eligible || allEligible || Boolean(action && ACTIVE.has(action.status))} checked={selected.has(item.id)} onChange={(event) => toggle(item.id, event.target.checked)} />
                  <span><strong>{item.name || item.id}</strong><small>{item.id} · {kind === "containers" ? item.state : kind === "volumes" ? `${item.driver} · ${item.references} references` : `${item.references} containers`}</small></span>
                  <span>{bytes(item.size_bytes)}{kind === "images" && <small> · {bytes(item.unique_bytes || 0)} unique</small>}</span>
                </label>)}</div>
                {listing && <div className="pagination"><span>Page {listing.page} · {listing.total} items</span><div><button disabled={page === 1} onClick={() => setPage(page - 1)}>Previous</button><button disabled={page * listing.limit >= listing.total} onClick={() => setPage(page + 1)}>Next</button></div></div>}
              </>}
            <div className="docker-cleanup-actions"><button className="danger-button" disabled={Boolean(action && ACTIVE.has(action.status)) || (kind !== "cache" && !allEligible && selected.size === 0)} onClick={() => void makePreview()}><Trash2 />Review {kind === "cache" ? "cache cleanup" : `selected ${kind}`}</button>
              {allEligible && <button onClick={() => setAllEligible(false)}>Clear all-eligible selection</button>}</div>
          </>}
    </section>
    {preview && <section className="panel docker-review" role="dialog" aria-label="Review Docker cleanup"><ShieldAlert /><div><h3>Review {preview.kind} cleanup</h3>
      {preview.kind === "cache" && preview.cache_summary ? <>
        <p>{preview.items.length} cache records selected · unused for over {preview.older_than_days} days.</p>
        <p>{preview.cache_summary.matching_count} age-matching records · {bytes(preview.cache_summary.matching_bytes)} total record size.</p>
        <p>{preview.cache_summary.retained_count} records ({bytes(preview.cache_summary.retained_bytes)}) retained by cache outside this selection.</p>
        <p>Selected cache size: {bytes(preview.cache_summary.private_bytes)} private · {bytes(preview.cache_summary.shared_bytes)} shared with images.</p>
        {preview.cache_summary.unknown_shared_bytes > 0 && <p>Sharing information unavailable for {bytes(preview.cache_summary.unknown_shared_bytes)}.</p>}
        <p>Private cache size is a potential saving, not a promise of filesystem space. Image-shared layers stay.
          Docker may retain records whose references change.</p>
      </> : <p>{preview.items.length} items · {bytes(preview.estimated_bytes)} estimated. Actual free space may differ.</p>}
      {preview.kind === "volumes" && <p><strong>Volume data will be permanently deleted and cannot be recovered from Docker.</strong></p>}
      <div className="docker-review-items">{preview.items.map((item) => <div key={item.id}>{item.name || item.id} · {item.id}</div>)}</div>
      {preview.items.length > 0 ? <>
        <label>Type <code>{preview.confirmation_phrase}</code><input value={phrase} onChange={(event) => setPhrase(event.target.value)} autoComplete="off" /></label>
        <div className="button-row"><button onClick={() => setPreview(null)}>Cancel</button><button className="danger-button" disabled={phrase !== preview.confirmation_phrase} onClick={() => void confirmCleanup()}>Schedule cleanup</button></div>
      </> : <><p>No cache records can currently be removed within this age selection.</p><button onClick={() => setPreview(null)}>Close</button></>}
    </div></section>}
    {action && <section className="panel docker-action-status" role="status"><strong>Docker {action.kind}: {action.status}</strong>
      <span>{action.status === "scheduled" ? `Starts in about ${countdown} seconds. You can cancel before it starts.` : action.error ||
        (action.kind === "cache" && action.outcomes.length > 0 ? action.outcomes.map(cacheOutcome).join(" ") :
          `${action.outcomes.filter((item) => item.status === "deleted").length} removed, ${action.outcomes.filter((item) => item.status !== "deleted").length} skipped or failed.`)}</span>
      {action.status === "scheduled" && action.undo_available && countdown > 0 && <button onClick={() => void undo()}>Cancel cleanup</button>}
      {!ACTIVE.has(action.status) && <button onClick={() => setAction(null)}>Dismiss</button>}
    </section>}
    {recent.length > 0 && <section className="panel storage-overview"><h3>Recent Docker activity</h3>
      {recent.map((entry) => <details key={entry.id} className="docker-history-row"><summary>{entry.kind} · {entry.status} · {entry.items.length} reviewed <small>{new Date(entry.created_at).toLocaleString()}</small></summary>
        {entry.error && <p>{entry.error}</p>}
        {entry.outcomes.map((outcome) => <p key={outcome.id}>{entry.kind === "cache" ? cacheOutcome(outcome) :
          `${outcome.id}: ${outcome.status}${outcome.error ? ` · ${outcome.error}` : ""}`}</p>)}
      </details>)}
    </section>}
  </section>;
}

function cacheOutcome(outcome: DockerOutcome): string {
  const count = outcome.deleted_count !== undefined
    ? `${outcome.deleted_count} cache records removed; ${outcome.not_removed_count ?? 0} retained by Docker. `
    : `Cache prune ${outcome.status}. `;
  const reclaimed = outcome.reclaimed_bytes !== undefined
    ? `Docker-reported reclaimed: ${bytes(outcome.reclaimed_bytes)}. Filesystem free-space change may differ. `
    : "";
  return count + reclaimed + (outcome.error || "");
}

function bytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let n = value; let index = 0;
  while (n >= 1024 && index < units.length - 1) { n /= 1024; index++; }
  return `${n.toFixed(n >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
}
