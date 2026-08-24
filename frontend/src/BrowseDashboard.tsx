import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowUpDown,
  ChevronLeft,
  ChevronRight,
  File,
  Folder,
  FolderOpen,
  HardDrive,
  Home,
  Info,
  Link2,
  Loader2,
  Monitor,
  RefreshCw,
  Search,
  ShieldCheck,
  ShieldAlert,
  Trash2,
  X,
  XCircle
} from "lucide-react";

import {
  cancelBrowseScan,
  createBrowseScan,
  getBrowseResult,
  listProtectedFolders,
  openBrowseEvents,
  previewBrowseDeletion,
  scheduleBrowseDeletion
} from "./api";
import type {
  BrowseDeletion,
  BrowseDeletionPreview,
  BrowseEntry,
  BrowseListing,
  BrowseResult,
  BrowseScan,
  DirectoryLocation,
  ProtectedFolder
} from "./types";

const PAGE_SIZE = 50;
const CACHE_LIMIT = 32;

interface CachedBrowse {
  result: BrowseResult;
  scan: BrowseScan;
}

const browseCache = new Map<string, CachedBrowse>();

type SortKey = "name" | "kind" | "allocated" | "logical";
type SortDirection = "asc" | "desc";
type ProtectionStatus = "loading" | "available" | "unavailable";

export interface BrowseProtectionSeed {
  name: string;
  sourcePath: string;
  returnPath: string;
}

export function clearBrowseCache(): void {
  browseCache.clear();
}

export function BrowseDashboard({
  token,
  initialPath,
  onPathChanged,
  onProtect,
  onOpenProtected,
  onOpenHelp,
  activeDeletion = null,
  refreshVersion = 0,
  onDeletionScheduled = () => undefined
}: {
  token: string;
  initialPath?: string;
  onPathChanged: (path: string) => void;
  onProtect: (seed: BrowseProtectionSeed) => void;
  onOpenProtected: (folderId: string, returnPath: string) => void;
  onOpenHelp: (article?: string) => void;
  activeDeletion?: BrowseDeletion | null;
  refreshVersion?: number;
  onDeletionScheduled?: (deletion: BrowseDeletion) => void;
}) {
  const [scan, setScan] = useState<BrowseScan | null>(null);
  const [listing, setListing] = useState<BrowseListing | null>(null);
  const [result, setResult] = useState<BrowseResult | null>(null);
  const [protectedFolders, setProtectedFolders] = useState<ProtectedFolder[]>([]);
  const [protectionStatus, setProtectionStatus] = useState<ProtectionStatus>("loading");
  const [pathInput, setPathInput] = useState(initialPath || "");
  const [showHidden, setShowHidden] = useState(false);
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("allocated");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [page, setPage] = useState(0);
  const [error, setError] = useState("");
  const [selectedPaths, setSelectedPaths] = useState<Set<string>>(new Set());
  const [deletionPreview, setDeletionPreview] = useState<BrowseDeletionPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [schedulingDeletion, setSchedulingDeletion] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const sourceRef = useRef<EventSource | null>(null);
  const activeScanRef = useRef<BrowseScan | null>(null);
  const generationRef = useRef(0);
  const currentPathRef = useRef(initialPath || "");
  const refreshVersionRef = useRef(refreshVersion);

  useEffect(() => {
    setProtectionStatus("loading");
    listProtectedFolders(token)
      .then((folders) => {
        setProtectedFolders(folders);
        setProtectionStatus("available");
      })
      .catch((err) => {
        setProtectionStatus("unavailable");
        setError(err instanceof Error ? err.message : "Protected-folder status is unavailable.");
      });
  }, [token]);

  useEffect(() => {
    if (refreshVersionRef.current === refreshVersion) return;
    refreshVersionRef.current = refreshVersion;
    void openPath(currentPathRef.current || undefined, true);
  }, [refreshVersion]);

  useEffect(() => {
    if (!activeDeletion || !["scheduled", "queued", "running"].includes(activeDeletion.status)) return;
    setSelectedPaths(new Set());
    setDeletionPreview(null);
    setPreviewError("");
  }, [activeDeletion?.id, activeDeletion?.status]);

  useEffect(() => {
    void openPath(initialPath || undefined);
    return () => {
      generationRef.current += 1;
      sourceRef.current?.close();
      const active = activeScanRef.current;
      if (active && ["queued", "running"].includes(active.status)) {
        void cancelBrowseScan(token, active.id).catch(() => undefined);
      }
    };
  }, [token]);

  async function stopActiveScan() {
    sourceRef.current?.close();
    sourceRef.current = null;
    const active = activeScanRef.current;
    activeScanRef.current = null;
    if (active && ["queued", "running"].includes(active.status)) {
      await cancelBrowseScan(token, active.id).catch(() => undefined);
    }
  }

  async function openPath(path?: string, force = false) {
    const generation = ++generationRef.current;
    await stopActiveScan();
    setError("");
    setQuery("");
    setPage(0);
    setSelectedPaths(new Set());
    setDeletionPreview(null);
    setPreviewError("");

    if (path && !force) {
      const cached = getCached(path);
      if (cached) {
        activeScanRef.current = null;
        setScan(cached.scan);
        setListing(cached.result);
        setResult(cached.result);
        setPathInput(cached.result.current_path);
        currentPathRef.current = cached.result.current_path;
        onPathChanged(cached.result.current_path);
        return;
      }
    }

    setScan(null);
    setResult(null);
    setListing(null);
    try {
      const created = await createBrowseScan(token, path);
      if (generation !== generationRef.current) {
        void cancelBrowseScan(token, created.scan.id).catch(() => undefined);
        return;
      }
      setListing(created.listing);
      setPathInput(created.listing.current_path);
      currentPathRef.current = created.listing.current_path;
      onPathChanged(created.listing.current_path);
      setScan(created.scan);
      activeScanRef.current = created.scan;
      if (created.scan.status === "completed") {
        activeScanRef.current = null;
        await loadCompletedResult(created.scan, generation);
        return;
      }
      const source = openBrowseEvents(token, created.scan.id, (next) => {
        if (generation !== generationRef.current) return;
        setScan(next);
        activeScanRef.current = next;
        if (next.status === "completed") {
          activeScanRef.current = null;
          void loadCompletedResult(next, generation);
        } else if (next.status === "failed") {
          activeScanRef.current = null;
          setError(next.error || next.progress_message);
        } else if (next.status === "cancelled") {
          activeScanRef.current = null;
        }
      });
      source.onerror = () => {
        if (generation === generationRef.current) {
          setError("Live sizing progress was interrupted; refresh this folder to reconnect.");
        }
      };
      sourceRef.current = source;
    } catch (err) {
      if (generation === generationRef.current) {
        setError(err instanceof Error ? err.message : "The folder could not be opened.");
      }
    }
  }

  async function loadCompletedResult(completed: BrowseScan, generation: number) {
    try {
      const next = await getBrowseResult(token, completed.id);
      if (generation !== generationRef.current) return;
      putCached(next, completed);
      setResult(next);
      setListing(next);
      setScan(completed);
      setPathInput(next.current_path);
      currentPathRef.current = next.current_path;
      onPathChanged(next.current_path);
    } catch (err) {
      if (generation === generationRef.current) {
        setError(err instanceof Error ? err.message : "Folder sizes could not be loaded.");
      }
    }
  }

  async function cancelCurrentScan() {
    const active = activeScanRef.current;
    if (!active) return;
    try {
      const next = await cancelBrowseScan(token, active.id);
      setScan(next);
      activeScanRef.current = next;
    } catch (err) {
      setError(err instanceof Error ? err.message : "The scan could not be cancelled.");
    }
  }

  function changeSort(nextKey: SortKey) {
    if (nextKey === sortKey) {
      setSortDirection((current) => current === "asc" ? "desc" : "asc");
    } else {
      setSortKey(nextKey);
      setSortDirection(nextKey === "name" || nextKey === "kind" ? "asc" : "desc");
    }
    setPage(0);
  }

  function toggleSelected(path: string, selected: boolean) {
    setSelectedPaths((current) => {
      const next = new Set(current);
      if (selected) next.add(path);
      else next.delete(path);
      return next;
    });
  }

  async function openDeletionReview() {
    if (!scan || scan.status !== "completed" || selectedPaths.size === 0) return;
    setPreviewLoading(true);
    setPreviewError("");
    try {
      setDeletionPreview(
        await previewBrowseDeletion(token, scan.id, Array.from(selectedPaths))
      );
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : "The deletion preview could not be created.");
    } finally {
      setPreviewLoading(false);
    }
  }

  async function confirmDeletion() {
    if (!deletionPreview?.can_schedule) return;
    setSchedulingDeletion(true);
    setPreviewError("");
    try {
      const scheduled = await scheduleBrowseDeletion(token, deletionPreview.id);
      setDeletionPreview(null);
      setSelectedPaths(new Set());
      onDeletionScheduled(scheduled);
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : "Deletion could not be scheduled.");
    } finally {
      setSchedulingDeletion(false);
    }
  }

  const rows = result?.entries || listing?.entries || [];
  const filteredRows = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    const visible = rows.filter((entry) => (
      (showHidden || !entry.hidden)
      && (!normalized || entry.name.toLowerCase().includes(normalized))
    ));
    return [...visible].sort((first, second) => {
      let comparison = 0;
      if (sortKey === "name") comparison = first.name.localeCompare(second.name);
      else if (sortKey === "kind") comparison = first.kind.localeCompare(second.kind) || first.name.localeCompare(second.name);
      else {
        const field = sortKey === "allocated" ? "allocated_bytes" : "logical_bytes";
        const left = first[field] ?? -1;
        const right = second[field] ?? -1;
        comparison = left - right || first.name.localeCompare(second.name);
      }
      return sortDirection === "asc" ? comparison : -comparison;
    });
  }, [rows, query, showHidden, sortKey, sortDirection]);
  const pageCount = Math.max(1, Math.ceil(filteredRows.length / PAGE_SIZE));
  const currentPage = Math.min(page, pageCount - 1);
  const visibleRows = filteredRows.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);
  const busy = scan?.status === "queued" || scan?.status === "running";
  const currentPath = listing?.current_path || pathInput || initialPath || "";
  const deletionActive = Boolean(
    activeDeletion && ["scheduled", "queued", "running"].includes(activeDeletion.status)
  );
  const pendingPaths = useMemo(() => new Set(
    deletionActive && activeDeletion
      ? activeDeletion.items.map((item) => item.path)
      : []
  ), [activeDeletion, deletionActive]);
  const selectedEntries = useMemo(() => (
    result?.entries.filter((entry) => selectedPaths.has(entry.path)) || []
  ), [result, selectedPaths]);
  const selectedAllocatedBytes = selectedEntries.reduce(
    (total, entry) => total + (entry.allocated_bytes || 0),
    0
  );
  const eligiblePageRows = visibleRows.filter((entry) => (
    Boolean(result)
    && entry.deletion_eligible
    && !deletionActive
    && !pendingPaths.has(entry.path)
  ));
  const allEligiblePageSelected = eligiblePageRows.length > 0
    && eligiblePageRows.every((entry) => selectedPaths.has(entry.path));
  const someEligiblePageSelected = eligiblePageRows.some((entry) => selectedPaths.has(entry.path));

  function toggleCurrentPage(selected: boolean) {
    setSelectedPaths((current) => {
      const next = new Set(current);
      for (const entry of eligiblePageRows) {
        if (selected) next.add(entry.path);
        else next.delete(entry.path);
      }
      return next;
    });
  }

  return (
    <section className="browse-dashboard">
      <header className="page-header">
        <div>
          <span className="eyebrow">One level at a time</span>
          <h2>Browse storage</h2>
          <p>Inspect storage, protect inactive projects, or permanently delete an exact reviewed selection with a 10-second Undo window.</p>
        </div>
        <div className="button-row">
          <button onClick={() => onOpenHelp("browse")}><Info />How sizing works</button>
          <button disabled={!listing || busy} onClick={() => void openPath(currentPath, true)}><RefreshCw />Refresh</button>
        </div>
      </header>

      {error && <div className="alert" role="alert">{error}</div>}
      <BrowseNavigator
        listing={listing}
        pathInput={pathInput}
        setPathInput={setPathInput}
        onOpen={(path) => void openPath(path)}
      />

      {busy && scan && (
        <section className="browse-progress panel" aria-live="polite">
          <Loader2 className="spin" />
          <div><strong>{scan.status === "queued" ? "Waiting for disk access" : "Calculating folder sizes"}</strong><span>{scan.progress_message}</span></div>
          <div className="progress-wrap" role="progressbar" aria-label="Browse sizing progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(scan.progress_percent)}><div style={{ width: `${scan.progress_percent}%` }} /></div>
          <button disabled={scan.cancel_requested} onClick={() => void cancelCurrentScan()}><XCircle />{scan.cancel_requested ? "Cancelling…" : "Cancel"}</button>
        </section>
      )}
      {scan?.status === "cancelled" && (
        <div className="advisory-banner"><AlertTriangle /><div><strong>Sizing was cancelled.</strong><span>Names and direct-file sizes remain visible; refresh to calculate folder totals.</span></div><button onClick={() => void openPath(currentPath, true)}>Start again</button></div>
      )}

      <BrowseSummary result={result} scan={scan} />

      <section className="panel browse-table-panel">
        <div className="browse-table-toolbar">
          <label className="search-field compact" htmlFor="browse-search"><Search /><span className="sr-only">Filter this folder</span><input id="browse-search" value={query} onChange={(event) => { setQuery(event.target.value); setPage(0); }} placeholder="Filter this level" /></label>
          <label className="checkbox-row compact-check"><input type="checkbox" checked={showHidden} onChange={(event) => { setShowHidden(event.target.checked); setPage(0); }} />Show hidden items</label>
          <span>{filteredRows.length} item{filteredRows.length === 1 ? "" : "s"}</span>
        </div>
        {selectedEntries.length > 0 && (
          <div className="browse-deletion-selection" role="status">
            <div><Trash2 /><span><strong>{selectedEntries.length} selected</strong><small>{formatBytes(selectedAllocatedBytes)} on disk</small></span></div>
            <div className="button-row">
              <button onClick={() => setSelectedPaths(new Set())}>Clear selection</button>
              <button className="danger-button" disabled={previewLoading || deletionActive || scan?.status !== "completed"} onClick={() => void openDeletionReview()}>
                {previewLoading ? <Loader2 className="spin" /> : <Trash2 />}
                {previewLoading ? "Reviewing…" : "Delete Permanently"}
              </button>
            </div>
          </div>
        )}
        {previewError && !deletionPreview && <div className="alert browse-delete-error" role="alert">{previewError}</div>}
        {result && !showHidden && result.hidden_entry_count > 0 && (
          <div className="hidden-summary"><Info /><span>{result.hidden_entry_count} hidden item{result.hidden_entry_count === 1 ? "" : "s"} {result.hidden_entry_count === 1 ? "uses" : "use"} {formatBytes(result.hidden_allocated_bytes)} on disk ({formatBytes(result.hidden_logical_bytes)} logical) and remain included in the total.</span><button onClick={() => setShowHidden(true)}>Show them</button></div>
        )}
        <div className="table-scroll browse-table-scroll">
          <table>
            <thead><tr>
              <SelectionHeader
                checked={allEligiblePageSelected}
                indeterminate={!allEligiblePageSelected && someEligiblePageSelected}
                disabled={eligiblePageRows.length === 0}
                onChange={toggleCurrentPage}
              />
              <SortableHeader label="Name" active={sortKey === "name"} direction={sortDirection} onClick={() => changeSort("name")} />
              <SortableHeader label="Type" active={sortKey === "kind"} direction={sortDirection} onClick={() => changeSort("kind")} />
              <SortableHeader label="On disk" active={sortKey === "allocated"} direction={sortDirection} onClick={() => changeSort("allocated")} />
              <SortableHeader label="Logical" active={sortKey === "logical"} direction={sortDirection} onClick={() => changeSort("logical")} />
              <th scope="col">Contents</th>
              <th scope="col">Protection</th>
            </tr></thead>
            <tbody>{visibleRows.map((entry) => {
              const relation = protectionRelation(entry.path, protectedFolders);
              const pending = pendingPaths.has(entry.path);
              const selectable = Boolean(result) && entry.deletion_eligible && !deletionActive && !pending;
              const selectionBlocker = pending
                ? "This item is pending deletion"
                : deletionActive
                  ? "Another permanent deletion is already active"
                  : !result
                    ? "Wait for sizing to finish"
                    : entry.deletion_blocker;
              const rowClasses = [entry.hidden ? "hidden-entry" : "", pending ? "pending-deletion-row" : ""].filter(Boolean).join(" ") || undefined;
              return (
                <tr key={entry.path} className={rowClasses}>
                  <td className="browse-select-cell">
                    <span className="browse-select-control">
                      <input
                        type="checkbox"
                        aria-label={`Select ${entry.name} for permanent deletion`}
                        checked={selectedPaths.has(entry.path)}
                        disabled={!selectable}
                        title={selectable ? `Select ${entry.name}` : selectionBlocker || "This item cannot be deleted"}
                        onChange={(event) => toggleSelected(entry.path, event.target.checked)}
                      />
                      {!selectable && !pending && selectionBlocker && (
                        <span
                          className="browse-delete-blocker-hint"
                          role="note"
                          tabIndex={0}
                          aria-label={`Cannot delete ${entry.name}: ${selectionBlocker}`}
                          title={selectionBlocker}
                        ><ShieldAlert aria-hidden="true" /></span>
                      )}
                    </span>
                  </td>
                  <td><EntryName entry={entry} pending={pending} onOpen={() => void openPath(entry.path)} /></td>
                  <td><span className={`entry-kind ${entry.kind}`}>{entry.kind}</span></td>
                  <td className="nowrap"><SizeValue entry={entry} field="allocated_bytes" /></td>
                  <td className="nowrap"><SizeValue entry={entry} field="logical_bytes" /></td>
                  <td className="browse-counts">{entry.kind === "directory" && entry.size_state !== "pending" ? <>{entry.file_count.toLocaleString()} files<small>{entry.directory_count.toLocaleString()} folders{entry.skipped_mount_count ? ` · ${entry.skipped_mount_count} mounts skipped` : ""}</small></> : "—"}</td>
                  <td>{pending ? <span className="pending-deletion-label"><Trash2 />Pending deletion</span> : <ProtectionAction entry={entry} relation={relation} protectionStatus={protectionStatus} currentPath={currentPath} onProtect={onProtect} onOpenProtected={onOpenProtected} />}</td>
                </tr>
              );
            })}</tbody>
          </table>
          {visibleRows.length === 0 && <p className="empty-table">No items match this view.</p>}
        </div>
        <div className="pagination"><span>Showing {filteredRows.length ? currentPage * PAGE_SIZE + 1 : 0}–{Math.min((currentPage + 1) * PAGE_SIZE, filteredRows.length)} of {filteredRows.length}</span><div><button disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}><ChevronLeft />Previous</button><span>Page {currentPage + 1} of {pageCount}</span><button disabled={currentPage >= pageCount - 1} onClick={() => setPage(currentPage + 1)}>Next<ChevronRight /></button></div></div>
      </section>

      {result && result.error_count > 0 && (
        <details className="panel browse-errors"><summary><AlertTriangle />{result.error_count} item{result.error_count === 1 ? "" : "s"} could not be fully measured</summary><ul>{result.errors.map((message, index) => <li key={`${message}-${index}`}>{message}</li>)}</ul></details>
      )}
      <div className="browse-footnote"><Info /><span>Allocated size uses filesystem block metadata and is not guaranteed reclaimable. APFS clones, hard links, snapshots, compression, permissions, and skipped mounts can make this total differ from macOS storage usage.</span></div>
      {deletionPreview && (
        <DeletionReviewModal
          preview={deletionPreview}
          error={previewError}
          scheduling={schedulingDeletion}
          onClose={() => { setDeletionPreview(null); setPreviewError(""); }}
          onConfirm={() => void confirmDeletion()}
        />
      )}
    </section>
  );
}

function BrowseNavigator({ listing, pathInput, setPathInput, onOpen }: {
  listing: BrowseListing | null;
  pathInput: string;
  setPathInput: (value: string) => void;
  onOpen: (path: string) => void;
}) {
  const breadcrumbs = buildBreadcrumbs(listing?.current_path || "");
  return <section className="browse-navigator panel">
    <div className="folder-roots" aria-label="Browse shortcuts">{listing?.roots.map((root) => <button key={root.path} disabled={!root.readable} onClick={() => onOpen(root.path)}><RootIcon root={root} /><span>{root.name}</span></button>)}</div>
    <nav className="breadcrumbs" aria-label="Current folder">{breadcrumbs.map((crumb) => <button key={crumb.path} title={crumb.path} onClick={() => onOpen(crumb.path)}>{crumb.name}</button>)}</nav>
    <form className="path-navigator" onSubmit={(event) => { event.preventDefault(); if (pathInput.trim()) onOpen(pathInput.trim()); }}>
      <button type="button" className="icon-button" disabled={!listing?.parent_path} onClick={() => listing?.parent_path && onOpen(listing.parent_path)} aria-label="Open parent folder"><ChevronLeft /></button>
      <label className="sr-only" htmlFor="browse-path">Folder path</label><input id="browse-path" value={pathInput} onChange={(event) => setPathInput(event.target.value)} />
      <button type="submit" disabled={!pathInput.trim()}>Go</button>
    </form>
  </section>;
}

function BrowseSummary({ result, scan }: { result: BrowseResult | null; scan: BrowseScan | null }) {
  const metrics = [
    ["On disk", result ? formatBytes(result.allocated_bytes) : "Calculating…"],
    ["Logical", result ? formatBytes(result.logical_bytes) : "Calculating…"],
    ["Files", result ? result.file_count.toLocaleString() : (scan?.files_scanned || 0).toLocaleString()],
    ["Folders", result ? result.directory_count.toLocaleString() : (scan?.directories_scanned || 0).toLocaleString()]
  ];
  const duration = scanDuration(scan);
  return <section className="browse-summary metric-grid">{metrics.map(([label, value]) => <div className="metric" key={label}><span>{label}</span><strong>{value}</strong>{label === "On disk" && result && <><small>Measured {formatDate(result.measured_at)}{duration ? ` · ${duration}` : ""}</small>{result.allocation_fallback_count > 0 && <small>{result.allocation_fallback_count} allocation fallback{result.allocation_fallback_count === 1 ? "" : "s"}</small>}{result.error_count > 0 && <small>Partial total · {result.error_count} scan error{result.error_count === 1 ? "" : "s"}</small>}</>}</div>)}</section>;
}

function SelectionHeader({ checked, indeterminate, disabled, onChange }: {
  checked: boolean;
  indeterminate: boolean;
  disabled: boolean;
  onChange: (checked: boolean) => void;
}) {
  const checkboxRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (checkboxRef.current) checkboxRef.current.indeterminate = indeterminate;
  }, [indeterminate]);
  return <th scope="col" className="browse-select-cell"><input ref={checkboxRef} type="checkbox" aria-label="Select eligible items on this page" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} /></th>;
}

function SortableHeader({ label, active, direction, onClick }: { label: string; active: boolean; direction: SortDirection; onClick: () => void }) {
  return <th scope="col" aria-sort={active ? (direction === "asc" ? "ascending" : "descending") : "none"}><button className={active ? "sort-button active" : "sort-button"} onClick={onClick}>{label}<ArrowUpDown /></button></th>;
}

function EntryName({ entry, pending, onOpen }: { entry: BrowseEntry; pending: boolean; onOpen: () => void }) {
  const icon = entry.kind === "directory" ? <Folder /> : entry.kind === "file" ? <File /> : entry.kind === "symlink" ? <Link2 /> : <FolderOpen />;
  const canOpen = entry.kind === "directory" && entry.readable && !entry.mount_boundary && !pending;
  return <div className="browse-entry-name">{icon}{canOpen ? <button title={entry.path} onClick={onOpen}>{entry.name}</button> : <span title={entry.path}>{entry.name}</span>}{entry.hidden && <small>hidden</small>}{entry.mount_boundary && <small>drive boundary</small>}{pending && <small className="pending-badge">pending deletion</small>}{entry.error_count > 0 && <AlertTriangle aria-label="Partial size" />}</div>;
}

function SizeValue({ entry, field }: { entry: BrowseEntry; field: "allocated_bytes" | "logical_bytes" }) {
  if (entry.size_state === "pending") return <span className="size-pending"><Loader2 className="spin" />Calculating</span>;
  if (entry.size_state === "unavailable") return <span className="muted">Unavailable</span>;
  if (entry.size_state === "boundary") return <span className="muted">Separate disk</span>;
  const value = entry[field];
  return <span title={entry.size_state === "partial" ? "Partial: some descendants could not be measured" : undefined}>{formatBytes(value || 0)}{entry.size_state === "partial" ? "*" : ""}</span>;
}

function ProtectionAction({ entry, relation, protectionStatus, currentPath, onProtect, onOpenProtected }: {
  entry: BrowseEntry;
  relation: ProtectionRelation | null;
  protectionStatus: ProtectionStatus;
  currentPath: string;
  onProtect: (seed: BrowseProtectionSeed) => void;
  onOpenProtected: (folderId: string, returnPath: string) => void;
}) {
  if (entry.kind === "file" || entry.kind === "other") return <span className="muted">—</span>;
  if (entry.kind === "symlink") return <span className="protection-blocked" title={entry.protection_blocker || undefined}>{entry.protection_blocker || "Symbolic links are not followed"}</span>;
  if (protectionStatus === "loading") return <span className="protection-blocked">Checking protected folders…</span>;
  if (protectionStatus === "unavailable") return <span className="protection-blocked" title="Reload Browse before starting protection">Protected-folder status unavailable</span>;
  if (relation?.kind === "exact") return <button className="compact-action protected" onClick={() => onOpenProtected(relation.folder.id, currentPath)}><ShieldCheck />Open protected folder</button>;
  if (relation) return <span className="protection-blocked" title={`Protected folders cannot overlap with ${relation.folder.name}`}><ShieldCheck />Overlaps {relation.folder.name}</span>;
  if (!entry.protection_eligible) return <span className="protection-blocked" title={entry.protection_blocker || undefined}>{entry.protection_blocker || "Not protectable"}</span>;
  return <button className="compact-action" onClick={() => onProtect({ name: entry.name, sourcePath: entry.path, returnPath: currentPath })}><ShieldCheck />Protect &amp; offload</button>;
}

function DeletionReviewModal({ preview, error, scheduling, onClose, onConfirm }: {
  preview: BrowseDeletionPreview;
  error: string;
  scheduling: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !scheduling) onClose();
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        "button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex='-1'])"
      ));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      opener?.focus();
    };
  }, [onClose, scheduling]);

  return (
    <div className="modal-backdrop" role="presentation">
      <section ref={dialogRef} className="deletion-review-modal" role="dialog" aria-modal="true" aria-labelledby="deletion-review-title" aria-describedby="deletion-review-description">
        <header className="modal-header">
          <div><span className="eyebrow">Permanent deletion</span><h2 id="deletion-review-title">Review the exact selection</h2></div>
          <button ref={closeRef} className="icon-button" disabled={scheduling} onClick={onClose} aria-label="Close deletion review"><X /></button>
        </header>
        <div className="deletion-warning" id="deletion-review-description"><ShieldAlert /><div><strong>This does not use Trash.</strong><span>The items remain in place for a 10-second Undo window, then are permanently removed.</span></div></div>
        <dl className="deletion-review-summary">
          <div><dt>Items</dt><dd>{preview.selected_count}</dd></div>
          <div><dt>On disk</dt><dd>{formatBytes(preview.allocated_bytes)}</dd></div>
          <div><dt>Logical</dt><dd>{formatBytes(preview.logical_bytes)}</dd></div>
        </dl>
        <div className="deletion-review-list" aria-label="Items selected for permanent deletion">
          {preview.items.map((item) => (
            <article key={item.path}><span className={`entry-kind ${item.kind}`}>{item.kind}</span><div><strong>{item.name}</strong><code>{item.path}</code></div><small>{formatBytes(item.allocated_bytes)}</small></article>
          ))}
        </div>
        {preview.blockers.length > 0 && (
          <div className="deletion-blockers" role="alert"><strong>Deletion is blocked until the selection is reviewed again.</strong><ul>{preview.blockers.map((blocker) => <li key={`${blocker.path}-${blocker.reason}`}><code>{blocker.path}</code><span>{blocker.reason}</span></li>)}</ul></div>
        )}
        {error && <div className="alert" role="alert">{error}</div>}
        <footer className="modal-actions">
          <button disabled={scheduling} onClick={onClose}>Cancel</button>
          <button className="danger-button" disabled={!preview.can_schedule || scheduling} onClick={onConfirm}>{scheduling ? <Loader2 className="spin" /> : <Trash2 />}{scheduling ? "Scheduling…" : "Schedule permanent deletion"}</button>
        </footer>
      </section>
    </div>
  );
}

type ProtectionRelation = { kind: "exact" | "inside" | "contains"; folder: ProtectedFolder };

function protectionRelation(path: string, folders: ProtectedFolder[]): ProtectionRelation | null {
  for (const folder of folders) {
    if (path === folder.source_path) return { kind: "exact", folder };
  }
  for (const folder of folders) {
    if (isUnder(path, folder.source_path)) return { kind: "inside", folder };
    if (isUnder(folder.source_path, path)) return { kind: "contains", folder };
  }
  return null;
}

function isUnder(path: string, root: string): boolean {
  const normalizedRoot = root === "/" ? "/" : root.replace(/\/$/, "");
  if (normalizedRoot === "/") return path !== "/" && path.startsWith("/");
  return path !== normalizedRoot && path.startsWith(`${normalizedRoot}/`);
}

function RootIcon({ root }: { root: DirectoryLocation }) {
  if (root.kind === "home") return <Home />;
  if (root.kind === "drive") return <HardDrive />;
  return <Monitor />;
}

function buildBreadcrumbs(path: string): Array<{ name: string; path: string }> {
  if (!path.startsWith("/")) return path ? [{ name: path, path }] : [];
  const crumbs = [{ name: "Computer", path: "/" }];
  let current = "";
  for (const part of path.split("/").filter(Boolean)) {
    current += `/${part}`;
    crumbs.push({ name: part, path: current });
  }
  return crumbs;
}

function getCached(path: string): CachedBrowse | null {
  const cached = browseCache.get(path);
  if (!cached) return null;
  browseCache.delete(path);
  browseCache.set(path, cached);
  return cached;
}

function putCached(result: BrowseResult, scan: BrowseScan): void {
  browseCache.delete(result.current_path);
  browseCache.set(result.current_path, { result, scan });
  while (browseCache.size > CACHE_LIMIT) {
    const oldest = browseCache.keys().next().value as string | undefined;
    if (!oldest) break;
    browseCache.delete(oldest);
  }
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${amount.toFixed(amount >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function scanDuration(scan: BrowseScan | null): string {
  if (!scan?.started_at || !scan.finished_at) return "";
  const milliseconds = new Date(scan.finished_at).getTime() - new Date(scan.started_at).getTime();
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return "";
  if (milliseconds < 1000) return "<1 s";
  return `${(milliseconds / 1000).toFixed(milliseconds < 10_000 ? 1 : 0)} s`;
}
