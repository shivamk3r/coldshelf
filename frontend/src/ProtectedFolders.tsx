import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  BookOpen,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Download,
  FolderCog,
  FolderOpen,
  HardDrive,
  History,
  Info,
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Settings,
  ShieldCheck,
  Trash2,
  Upload
} from "lucide-react";

import {
  cancelProtectionOperation,
  confirmRecoveryKey,
  createProtectedFolder,
  createProtectionOperation,
  deleteProtectedFolder,
  enrollProtectedDrive,
  getProtectedFolder,
  importProtectedFolder,
  listMountedVolumes,
  listProtectedFolders,
  openProtectionEvents,
  protectionPreflight,
  updateProtectedFolder
} from "./api";
import type { ProtectionOperationPayload } from "./api";
import type { ProtectionIntent } from "./App";
import { FolderBrowser } from "./FolderBrowser";
import type {
  DriveTarget,
  MountedVolume,
  ProtectedFolder,
  ProtectedFolderCreated,
  ProtectedFolderDetail,
  ProtectionOperation,
  ProtectionPreflight
} from "./types";

const CATALOG_PAGE_SIZE = 100;

export function ProtectedFolders({ token, intent = "manage", onOpenHelp = () => undefined }: {
  token: string;
  intent?: ProtectionIntent;
  onOpenHelp?: (article?: string) => void;
}) {
  const [preflight, setPreflight] = useState<ProtectionPreflight | null>(null);
  const [folders, setFolders] = useState<ProtectedFolder[]>([]);
  const [activeId, setActiveId] = useState("");
  const [detail, setDetail] = useState<ProtectedFolderDetail | null>(null);
  const [volumes, setVolumes] = useState<MountedVolume[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [catalogOffset, setCatalogOffset] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [operation, setOperation] = useState<ProtectionOperation | null>(null);
  const [created, setCreated] = useState<ProtectedFolderCreated | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showRestore, setShowRestore] = useState(false);
  const [evictionPreview, setEvictionPreview] = useState<Record<string, unknown> | null>(null);
  const [evictionPhrase, setEvictionPhrase] = useState("");
  const [previewAll, setPreviewAll] = useState(false);
  const [previewFileIds, setPreviewFileIds] = useState<string[]>([]);
  const [recoveryPaste, setRecoveryPaste] = useState("");
  const [verifyDriveId, setVerifyDriveId] = useState("");
  const sourceRef = useRef<EventSource | null>(null);

  const busy = operation?.status === "queued" || operation?.status === "running";
  const connectedDrives = detail?.drives.filter((drive) => drive.mounted) || [];
  const enrolledUuids = new Set(detail?.drives.map((drive) => drive.volume_uuid) || []);
  const enrollable = volumes.filter((volume) => !enrolledUuids.has(volume.volume_uuid));
  const catalog = detail?.catalog || { query, limit: CATALOG_PAGE_SIZE, offset: catalogOffset, total: detail?.files.length || 0 };
  const pageStart = catalog.total ? catalog.offset + 1 : 0;
  const pageEnd = Math.min(catalog.offset + (detail?.files.length || 0), catalog.total);

  useEffect(() => {
    if (intent === "create") setShowCreate(true);
    if (intent === "recover") setShowImport(true);
    if (intent === "restore") setShowRestore(true);
  }, [intent]);

  useEffect(() => {
    protectionPreflight(token)
      .then(setPreflight)
      .catch((err) => setError(err.message));
  }, [token]);

  useEffect(() => {
    refresh().catch((err) => setError(err.message));
    const interval = window.setInterval(() => {
      refresh().catch((err) => setError(err.message));
    }, 5000);
    return () => {
      window.clearInterval(interval);
      sourceRef.current?.close();
    };
  }, [token, activeId, query, catalogOffset, preflight?.supported]);

  async function refresh(folderId = activeId, search = query, offset = catalogOffset) {
    const nextFolders = await listProtectedFolders(token);
    let mounted: MountedVolume[] = [];
    if (preflight?.supported !== false) {
      try {
        mounted = (await listMountedVolumes(token)).volumes;
      } catch (err) {
        if (preflight?.ready) throw err;
      }
    }
    setFolders(nextFolders);
    setVolumes(mounted);
    const nextId = folderId || nextFolders[0]?.id || "";
    if (!nextId) {
      setActiveId("");
      setDetail(null);
      return;
    }
    setActiveId(nextId);
    const nextDetail = await getProtectedFolder(token, nextId, search, CATALOG_PAGE_SIZE, offset);
    setDetail(nextDetail);
    if (!verifyDriveId) {
      const mountedDrive = nextDetail.drives.find((drive) => drive.mounted);
      if (mountedDrive) setVerifyDriveId(mountedDrive.id);
    }
    const running = nextDetail.operations.find((item) => item.status === "queued" || item.status === "running");
    if (running && running.id !== operation?.id) {
      setOperation(running);
      watchOperation(running.id);
    }
  }

  function watchOperation(operationId: string) {
    sourceRef.current?.close();
    const source = openProtectionEvents(token, operationId, (next) => {
      setOperation(next);
      if (["completed", "failed", "cancelled"].includes(next.status)) {
        source.close();
        if (next.status === "completed") {
          setNotice(`${humanize(next.kind)} completed`);
          if (next.kind === "evict" && next.request.dry_run === true && next.result) {
            setEvictionPreview(next.result);
          } else {
            setSelected(new Set());
          }
          refresh(activeId).catch((err) => setError(err.message));
        } else {
          setError(next.error || `${humanize(next.kind)} ${next.status}`);
        }
      }
    });
    source.onerror = () => setError("Live operation progress was interrupted; reconnecting.");
    sourceRef.current = source;
  }

  async function selectFolder(folderId: string) {
    setActiveId(folderId);
    setCatalogOffset(0);
    setQuery("");
    setQueryInput("");
    setSelected(new Set());
    setEvictionPreview(null);
    setShowSettings(false);
    setError("");
    setDetail(await getProtectedFolder(token, folderId, "", CATALOG_PAGE_SIZE, 0));
  }

  async function runOperation(payload: ProtectionOperationPayload) {
    if (!activeId) return;
    setError("");
    setNotice("");
    setEvictionPreview(null);
    const queued = await createProtectionOperation(token, activeId, payload);
    setOperation(queued);
    watchOperation(queued.id);
  }

  async function enroll(volume: MountedVolume) {
    if (!activeId) return;
    setError("");
    await enrollProtectedDrive(token, activeId, volume.mount_path);
    setNotice(`${volume.name} enrolled`);
    await refresh(activeId);
  }

  function toggleFile(fileId: string) {
    const next = new Set(selected);
    if (next.has(fileId)) next.delete(fileId);
    else next.add(fileId);
    setSelected(next);
  }

  return (
    <section className="protected-dashboard">
      <header className="page-header protected-header">
        <div>
          <span className="eyebrow">Independent encrypted archives</span>
          <h2>Protected folders</h2>
          <p>Catalog files offline, back up one disk at a time, and evict only after verified coverage.</p>
        </div>
        <div className="button-row">
          <button onClick={() => onOpenHelp("protect-setup")}><BookOpen />Protection guide</button>
          <button disabled={preflight?.ready !== true} title={preflight?.ready ? "" : preflight?.message || "Checking prerequisites"} onClick={() => setShowImport((value) => !value)}><Download />Recover</button>
          <button className="primary-inline" disabled={preflight?.ready !== true} title={preflight?.ready ? "" : preflight?.message || "Checking prerequisites"} onClick={() => setShowCreate((value) => !value)}><Plus />New folder</button>
        </div>
      </header>

      {error && <div className="alert" role="alert">{error}</div>}
      {notice && <div className="result-toast"><CheckCircle2 />{notice}</div>}
      <ReadinessPanel preflight={preflight} volumeCount={volumes.length} onHelp={() => onOpenHelp("protect-setup")} />

      {showCreate && (
        <CreateFolderForm
          token={token}
          ready={preflight?.ready === true}
          blockedReason={preflight?.message}
          created={created}
          setCreated={setCreated}
          onComplete={(folderId) => {
            setShowCreate(false);
            refresh(folderId).catch((err) => setError(err.message));
          }}
          onError={setError}
        />
      )}
      {showImport && (
        <RecoveryImportForm
          token={token}
          ready={preflight?.ready === true}
          blockedReason={preflight?.message}
          volumes={volumes}
          onComplete={(folderId) => {
            setShowImport(false);
            refresh(folderId).catch((err) => setError(err.message));
          }}
          onError={setError}
        />
      )}

      <div className="protected-layout">
        <nav className="protected-folder-list panel" aria-label="Protected folders">
          <div className="section-heading"><div><ShieldCheck /><h3>Folders</h3></div><span>{folders.length}</span></div>
          {folders.length === 0 && <p className="muted">No protected folders yet. Create one or recover a catalog from a disk.</p>}
          {folders.map((folder) => (
            <button key={folder.id} className={folder.id === activeId ? "active" : ""} onClick={() => selectFolder(folder.id).catch((err) => setError(err.message))}>
              <ShieldCheck /><span>{folder.name}<small>{shortPath(folder.source_path)}</small></span>
            </button>
          ))}
        </nav>

        <div className="protected-main">
          {!detail && <div className="empty-state panel"><ShieldCheck /><h2>Create or recover a protected folder</h2><p>The readiness card above explains any prerequisite that still needs attention.</p></div>}
          {detail && (
            <>
              <LifecycleChecklist detail={detail} connectedDrives={connectedDrives.length} />
              {!detail.folder.recovery_confirmed && (
                <div className="alert recovery-confirm-inline">
                  <div><strong>Confirm the saved recovery key</strong><span>Disk enrollment stays blocked until the one-time key has been saved and pasted back.</span></div>
                  <input aria-label="Recovery key confirmation" type="password" value={recoveryPaste} onChange={(event) => setRecoveryPaste(event.target.value)} />
                  <button disabled={!recoveryPaste} onClick={() => confirmRecoveryKey(token, detail.folder.id, recoveryPaste).then(() => { setRecoveryPaste(""); return refresh(detail.folder.id); }).catch((err) => setError(err.message))}>Confirm key</button>
                </div>
              )}
              {connectedDrives.length > 0 && detail.summary.pending_files > 0 && (
                <div className="pending-prompt"><AlertTriangle /><div><strong>{detail.summary.pending_files} files still need replica work.</strong><span>A registered disk is connected, so this is a good time to index and back up.</span></div></div>
              )}

              <section className="protected-title panel">
                <div><span className="eyebrow">{detail.folder.required_copies} verified repositories required</span><h2>{detail.folder.name}</h2><p>{detail.folder.source_path}</p></div>
                <div className="button-row">
                  <button disabled={busy} onClick={() => setShowSettings((value) => !value)}><Settings />Settings</button>
                  <button disabled={busy} onClick={() => runOperation({ kind: "index" }).catch((err) => setError(err.message))}><RefreshCw />Index now</button>
                </div>
              </section>

              {showSettings && (
                <FolderSettings
                  token={token}
                  folder={detail.folder}
                  onUpdated={() => { setNotice("Folder settings updated"); setShowSettings(false); refresh(activeId).catch((err) => setError(err.message)); }}
                  onDeleted={() => { setNotice("Protected catalog deleted; disk repositories were left intact"); setShowSettings(false); setActiveId(""); setDetail(null); refresh("").catch((err) => setError(err.message)); }}
                  onError={setError}
                />
              )}

              <SummaryMetrics detail={detail} />
              <section className="drive-grid">
                {detail.drives.map((drive) => <DriveCard key={drive.id} drive={drive} busy={busy} runOperation={runOperation} onError={setError} />)}
                {enrollable.map((volume) => (
                  <article className="drive-card enroll-card" key={volume.volume_uuid}>
                    <HardDrive /><span className="eyebrow">Available disk</span><h3>{volume.name}</h3><p>{volume.filesystem} · {formatBytes(volume.free_bytes)} free</p>
                    <button disabled={!detail.folder.recovery_confirmed || busy} title={!detail.folder.recovery_confirmed ? "Confirm the recovery key before enrollment" : ""} onClick={() => enroll(volume).catch((err) => setError(err.message))}>Enroll this disk</button>
                  </article>
                ))}
                {detail.drives.length === 0 && enrollable.length === 0 && <div className="empty-drive panel"><HardDrive /><strong>No enrolled or available external disks</strong><span>Connect a writable external disk, then wait a few seconds or refresh.</span></div>}
              </section>

              {operation && <OperationProgress operation={operation} busy={busy} token={token} onError={setError} />}

              <section className="catalog panel">
                <div className="catalog-header">
                  <div><h2>Offline file catalog</h2><span>{pageStart}–{pageEnd} of {catalog.total}</span></div>
                  <form onSubmit={(event) => { event.preventDefault(); setCatalogOffset(0); setQuery(queryInput); }}>
                    <label className="sr-only" htmlFor="catalog-search">Search relative path</label>
                    <input id="catalog-search" value={queryInput} onChange={(event) => setQueryInput(event.target.value)} placeholder="Search relative path" />
                    <button aria-label="Search catalog"><Search /></button>
                  </form>
                </div>
                <div className="catalog-help"><Info /><span>Coverage counts verified receipts for this exact file version. Structural and full checks both satisfy the default policy; full reads data blocks back.</span><button onClick={() => onOpenHelp("protect-checks")}>Learn more</button></div>
                <div className="table-toolbar">
                  <div className="button-row">
                    <button onClick={() => {
                      const allSelected = detail.files.length > 0 && detail.files.every((file) => selected.has(file.id));
                      const next = new Set(selected);
                      detail.files.forEach((file) => allSelected ? next.delete(file.id) : next.add(file.id));
                      setSelected(next);
                    }}>{detail.files.length > 0 && detail.files.every((file) => selected.has(file.id)) ? "Clear page" : "Select page"}</button>
                    {selected.size > 0 && <button onClick={() => setSelected(new Set())}>Clear selection</button>}
                  </div>
                  <span>{selected.size} selected</span>
                </div>
                <div className="catalog-table-wrap">
                  <table>
                    <thead><tr><th scope="col">Select</th><th scope="col">Path</th><th scope="col">Local state</th><th scope="col">Size</th><th scope="col">Coverage</th>{detail.drives.map((drive) => <th scope="col" key={drive.id}>{drive.volume_name}</th>)}</tr></thead>
                    <tbody>{detail.files.map((file) => (
                      <tr key={file.id}>
                        <td><input type="checkbox" checked={selected.has(file.id)} onChange={() => toggleFile(file.id)} aria-label={`Select ${file.relative_path}`} /></td>
                        <td className="catalog-path"><span title={file.relative_path}>{file.relative_path}</span></td>
                        <td><span className={`local-state ${file.local_state}`}>{file.local_state}</span></td>
                        <td className="nowrap">{formatBytes(file.size)}</td>
                        <td>{file.verified_replica_count}/{detail.folder.required_copies}</td>
                        {detail.drives.map((drive) => {
                          const replica = file.replicas.find((item) => item.drive_target_id === drive.id);
                          return <td key={drive.id}><ReplicaBadge status={replica?.status || "missing"} /></td>;
                        })}
                      </tr>
                    ))}</tbody>
                  </table>
                  {detail.files.length === 0 && <p className="empty-table">No catalog files match this search.</p>}
                </div>
                <div className="catalog-pagination">
                  <button disabled={catalog.offset === 0} onClick={() => setCatalogOffset(Math.max(0, catalog.offset - catalog.limit))}><ChevronLeft />Previous</button>
                  <span>Showing {pageStart}–{pageEnd} of {catalog.total}</span>
                  <button disabled={catalog.offset + catalog.limit >= catalog.total} onClick={() => setCatalogOffset(catalog.offset + catalog.limit)}>Next<ChevronRight /></button>
                </div>
                <div className="catalog-actions">
                  <button disabled={busy || selected.size === 0} onClick={() => { setPreviewAll(false); setPreviewFileIds([...selected]); runOperation({ kind: "evict", file_ids: [...selected], dry_run: true }).catch((err) => setError(err.message)); }}>Preview selected eviction</button>
                  <button disabled={busy || detail.summary.local_files === 0} onClick={() => { setPreviewAll(true); setPreviewFileIds([]); runOperation({ kind: "evict", all_files: true, dry_run: true }).catch((err) => setError(err.message)); }}>Preview all local eviction</button>
                  <select aria-label="Drive for selected full verification" value={verifyDriveId} onChange={(event) => setVerifyDriveId(event.target.value)} disabled={connectedDrives.length === 0}>
                    <option value="">Choose drive for full verification</option>
                    {connectedDrives.map((drive) => <option key={drive.id} value={drive.id}>{drive.volume_name}</option>)}
                  </select>
                  <button disabled={busy || selected.size === 0 || !verifyDriveId} title={selected.size === 0 ? "Select catalog files first" : !verifyDriveId ? "Choose a connected drive" : ""} onClick={() => runOperation({ kind: "verify", drive_target_id: verifyDriveId, full: true, file_ids: [...selected] }).catch((err) => setError(err.message))}><ShieldCheck />Full verify selected</button>
                  <button disabled={busy || selected.size === 0 || connectedDrives.length === 0} title={selected.size === 0 ? "Select catalog files first" : connectedDrives.length === 0 ? "Connect an enrolled drive" : ""} onClick={() => setShowRestore(true)}><Upload />Restore selected</button>
                </div>
                {showRestore && (
                  <RestorePanel
                    token={token}
                    selectedCount={selected.size}
                    connectedDrives={connectedDrives}
                    busy={busy}
                    onClose={() => setShowRestore(false)}
                    onRestore={(driveId, destination) => {
                      setShowRestore(false);
                      runOperation({ kind: "restore", file_ids: [...selected], ...(driveId ? { drive_target_id: driveId } : {}), ...(destination ? { destination } : {}) }).catch((err) => setError(err.message));
                    }}
                  />
                )}
              </section>

              {evictionPreview && (
                <section className="eviction-confirm panel">
                  <AlertTriangle />
                  <div><span className="eyebrow">Preview complete</span><h2>Confirm protected-file eviction</h2><p>{String(evictionPreview.verification_warning || "Data blocks were not necessarily read back bit-for-bit.")}</p><p><strong>{String(evictionPreview.eligible_count || 0)} eligible</strong> · {Array.isArray(evictionPreview.blocked) ? evictionPreview.blocked.length : 0} blocked</p>
                    {Array.isArray(evictionPreview.blocked) && evictionPreview.blocked.length > 0 && <details><summary>Why files are blocked</summary><ul>{evictionPreview.blocked.map((item, index) => <li key={index}>{typeof item === "string" ? item : JSON.stringify(item)}</li>)}</ul></details>}
                    <label htmlFor="eviction-phrase">Type this exact confirmation phrase</label><code className="confirmation-code">{String(evictionPreview.confirmation_phrase || "")}</code><input id="eviction-phrase" value={evictionPhrase} onChange={(event) => setEvictionPhrase(event.target.value)} />
                    <button className="danger-button" disabled={busy || evictionPhrase !== evictionPreview.confirmation_phrase || Number(evictionPreview.eligible_count || 0) === 0 || (Array.isArray(evictionPreview.blocked) && evictionPreview.blocked.length > 0)} onClick={() => runOperation({ kind: "evict", ...(previewAll ? { all_files: true } : { file_ids: previewFileIds }), dry_run: false, confirmation_phrase: evictionPhrase }).catch((err) => setError(err.message))}>Evict eligible files now</button>
                  </div>
                </section>
              )}

              <OperationHistory operations={detail.operations} />
            </>
          )}
        </div>
      </div>
    </section>
  );
}

function ReadinessPanel({ preflight, volumeCount, onHelp }: { preflight: ProtectionPreflight | null; volumeCount: number; onHelp: () => void }) {
  if (!preflight) return <section className="readiness-panel panel"><Loader2 className="spin" /><div><strong>Checking protected-folder prerequisites…</strong></div></section>;
  return <section className={preflight.ready ? "readiness-panel panel ready" : "readiness-panel panel blocked"}>{preflight.ready ? <CheckCircle2 /> : <AlertTriangle />}<div><span className="eyebrow">System readiness</span><strong>{preflight.ready ? "Ready for protected folders" : "Setup needed"}</strong><p>{preflight.message}</p><div className="readiness-facts"><span>Platform: {preflight.platform}</span><span>Restic: {preflight.restic_version || `requires ${preflight.minimum_restic_version}+`}</span><span>Available disks: {volumeCount}</span><span>Recovery key: save and confirm before enrollment</span></div></div><button onClick={onHelp}>Setup instructions</button></section>;
}

function LifecycleChecklist({ detail, connectedDrives }: { detail: ProtectedFolderDetail; connectedDrives: number }) {
  const hasIndex = Boolean(detail.folder.current_generation_id);
  const hasBackup = detail.drives.some((drive) => Boolean(drive.last_backup_at));
  const hasVerification = detail.drives.some((drive) => Boolean(drive.last_structural_check_at || drive.last_full_check_at));
  const enoughCoverage = detail.summary.file_count > 0 && detail.summary.pending_files === 0;
  const hasRestore = detail.operations.some((operation) => operation.kind === "restore" && operation.status === "completed");
  const steps = [
    ["Protected catalog created", true],
    ["Recovery key saved and confirmed", detail.folder.recovery_confirmed],
    ["At least one disk enrolled", detail.drives.length > 0],
    ["Local catalog indexed", hasIndex],
    ["Current generation backed up", hasBackup],
    ["At least one repository verified", hasVerification],
    [`${detail.folder.required_copies} verified copies reached`, enoughCoverage],
    ["Optional: eligible files evicted", detail.summary.evicted_files > 0],
    ["Restore completed when needed", hasRestore]
  ] as const;
  return <section className="lifecycle panel"><div><span className="eyebrow">Protection lifecycle</span><h2>Your next safe step</h2><p>{connectedDrives > 0 ? `${connectedDrives} enrolled drive${connectedDrives === 1 ? " is" : "s are"} connected.` : "Connect an enrolled drive for backup, verification, or restore."}</p></div><ol>{steps.map(([label, done], index) => <li className={done ? "done" : ""} key={label}><span>{done ? <CheckCircle2 /> : index + 1}</span>{label}</li>)}</ol></section>;
}

function SummaryMetrics({ detail }: { detail: ProtectedFolderDetail }) {
  const summary = detail.summary;
  const metrics = [
    ["Local", summary.local_files, summary.local_bytes],
    ["No copies", summary.unprotected_files, summary.unprotected_bytes],
    ["Partial", summary.partial_files, summary.partial_bytes],
    ["Protected", summary.protected_files, summary.protected_bytes],
    ["Evicted", summary.evicted_files, summary.evicted_bytes],
    ["At risk", summary.at_risk_files, summary.at_risk_bytes]
  ] as const;
  return <section className="protection-metrics">{metrics.map(([label, files, bytes]) => <div className={label === "At risk" && files ? "metric risk" : "metric"} key={label}><span>{label}</span><strong>{files} files</strong><small>{formatBytes(bytes)}</small></div>)}</section>;
}

function DriveCard({ drive, busy, runOperation, onError }: { drive: DriveTarget; busy: boolean; runOperation: (payload: ProtectionOperationPayload) => Promise<void>; onError: (message: string) => void }) {
  return <article className="drive-card"><div className="drive-card-title"><HardDrive /><h3>{drive.volume_name}</h3><span className={drive.mounted ? "drive-online" : "drive-offline"}>{drive.mounted ? "mounted" : "offline"}</span></div><dl><dt>Repository</dt><dd title={drive.repository_id}>{drive.repository_id.slice(0, 12)}…</dd><dt>Capacity</dt><dd>{formatBytes(drive.free_bytes)} free / {formatBytes(drive.total_bytes)}</dd><dt>Last backup</dt><dd>{formatDate(drive.last_backup_at)}</dd><dt>Structural</dt><dd>{formatDate(drive.last_structural_check_at)}</dd><dt>Full read</dt><dd>{formatDate(drive.last_full_check_at)}</dd></dl><div className="button-row"><button disabled={busy || !drive.mounted} title={!drive.mounted ? "Connect this enrolled disk to back up" : ""} onClick={() => runOperation({ kind: "backup", drive_target_id: drive.id }).catch((err) => onError(err.message))}>Back up</button><button disabled={busy || !drive.mounted} onClick={() => runOperation({ kind: "verify", drive_target_id: drive.id }).catch((err) => onError(err.message))}>Structural check</button><button disabled={busy || !drive.mounted} onClick={() => runOperation({ kind: "verify", drive_target_id: drive.id, full: true }).catch((err) => onError(err.message))}>Full check</button></div></article>;
}

function OperationProgress({ operation, busy, token, onError }: { operation: ProtectionOperation; busy: boolean; token: string; onError: (message: string) => void }) {
  return <section className="operation-panel panel"><div><strong>{humanize(operation.kind)}</strong><span className={`status-pill ${operation.status}`}>{operation.status}</span><p>{operation.progress_message}</p>{operation.kind === "backup" && busy && operation.eta_seconds != null && <div className="operation-eta"><p>About {formatEtaDuration(operation.eta_seconds)} of file transfer remaining.</p><small>A structural check follows automatically.</small></div>}</div><div><span className="progress-label">{Math.round(operation.progress_percent)}%</span><div className="progress-wrap"><div style={{ width: `${operation.progress_percent}%` }} /></div></div>{busy && <button onClick={() => cancelProtectionOperation(token, operation.id).catch((err) => onError(err.message))}>Cancel safely</button>}</section>;
}

function FolderSettings({ token, folder, onUpdated, onDeleted, onError }: { token: string; folder: ProtectedFolder; onUpdated: () => void; onDeleted: () => void; onError: (message: string) => void }) {
  const [name, setName] = useState(folder.name);
  const [copies, setCopies] = useState(folder.required_copies);
  const [updatePhrase, setUpdatePhrase] = useState("");
  const [deletePhrase, setDeletePhrase] = useState("");
  const loweringPhrase = `UPDATE COPIES ${folder.name} ${copies}`;
  const deleteExpected = `DELETE PROTECTED ${folder.name}`;
  const lowering = copies < folder.required_copies;
  return <section className="settings-panel panel"><div className="section-heading"><div><FolderCog /><h2>Folder settings</h2></div><span>Source path cannot be changed</span></div><div className="form-grid"><div className="field-group"><label htmlFor="settings-name">Display name</label><input id="settings-name" value={name} onChange={(event) => setName(event.target.value)} /></div><div className="field-group"><label htmlFor="settings-copies">Required verified copies</label><input id="settings-copies" type="number" min="1" max="10" value={copies} onChange={(event) => setCopies(Number(event.target.value))} /></div>{lowering && <div className="field-group field-span-2 warning-field"><AlertTriangle /><p>Lowering the copy requirement weakens the eviction gate. Type <code>{loweringPhrase}</code> to confirm.</p><input aria-label="Lower copy count confirmation" value={updatePhrase} onChange={(event) => setUpdatePhrase(event.target.value)} /></div>}</div><button className="primary-inline" disabled={!name.trim() || copies < 1 || copies > 10 || (lowering && updatePhrase !== loweringPhrase)} onClick={() => updateProtectedFolder(token, folder.id, { name: name.trim(), required_copies: copies, ...(lowering ? { confirmation_phrase: updatePhrase } : {}) }).then(onUpdated).catch((err) => onError(err.message))}>Save settings</button><details className="danger-zone"><summary><Trash2 />Delete protected catalog</summary><p>This removes the local catalog and Keychain secret only. Encrypted repositories on enrolled disks remain intact. Deletion is blocked while any catalog file is evicted.</p><code className="confirmation-code">{deleteExpected}</code><input aria-label="Delete protected catalog confirmation" value={deletePhrase} onChange={(event) => setDeletePhrase(event.target.value)} /><button className="danger-button" disabled={deletePhrase !== deleteExpected} onClick={() => deleteProtectedFolder(token, folder.id, deletePhrase).then(onDeleted).catch((err) => onError(err.message))}>Delete catalog only</button></details></section>;
}

function CreateFolderForm({ token, ready, blockedReason, created, setCreated, onComplete, onError }: { token: string; ready: boolean; blockedReason?: string; created: ProtectedFolderCreated | null; setCreated: (folder: ProtectedFolderCreated | null) => void; onComplete: (folderId: string) => void; onError: (message: string) => void }) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [copies, setCopies] = useState(2);
  const [confirmation, setConfirmation] = useState("");
  const [browse, setBrowse] = useState(false);
  if (created) return <section className="recovery-key-panel panel"><KeyRound /><div><span className="eyebrow">One-time display</span><h2>Save this recovery key now</h2><p>Store it in your password manager. It will not be shown by the app again.</p><code>{created.recovery_key}</code><details><summary>Standalone recovery instructions</summary><pre>{created.recovery_instructions}</pre></details><label htmlFor="recovery-confirm">Paste the key to confirm it was saved</label><input id="recovery-confirm" type="password" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /><button disabled={confirmation !== created.recovery_key} onClick={() => confirmRecoveryKey(token, created.id, confirmation).then(() => { const id = created.id; setCreated(null); onComplete(id); }).catch((err) => onError(err.message))}>Confirm recovery key</button></div></section>;
  return <><form className="protected-form panel" onSubmit={(event) => { event.preventDefault(); createProtectedFolder(token, { name, source_path: path, required_copies: copies }).then(setCreated).catch((err) => onError(err.message)); }}><div className="section-heading"><div><Plus /><h2>Create protected folder</h2></div><span>macOS + Restic</span></div><div className="field-group"><label htmlFor="protected-name">Name</label><input id="protected-name" value={name} onChange={(event) => setName(event.target.value)} required /></div><div className="field-group"><label htmlFor="protected-path">Mac folder path</label><div className="input-with-button"><input id="protected-path" value={path} onChange={(event) => setPath(event.target.value)} required placeholder="/Users/you/large-files" /><button type="button" onClick={() => setBrowse(true)}><FolderOpen />Browse</button></div></div><div className="field-group"><label htmlFor="protected-copies">Required independent copies</label><input id="protected-copies" type="number" min="1" max="10" value={copies} onChange={(event) => setCopies(Number(event.target.value))} /><small>Two separately stored disks are recommended.</small></div><button className="primary-inline" disabled={!ready} title={ready ? "" : blockedReason || "Checking prerequisites"}><Plus />Create and generate key</button></form>{browse && <FolderBrowser token={token} initialPath={path} title="Choose the local folder to protect" onClose={() => setBrowse(false)} onSelect={(next) => { setPath(next); setBrowse(false); }} />}</>;
}

function RecoveryImportForm({ token, ready, blockedReason, volumes, onComplete, onError }: { token: string; ready: boolean; blockedReason?: string; volumes: MountedVolume[]; onComplete: (folderId: string) => void; onError: (message: string) => void }) {
  const [name, setName] = useState("Recovered protected folder");
  const [sourcePath, setSourcePath] = useState("");
  const [mountPath, setMountPath] = useState(volumes[0]?.mount_path || "");
  const [setId, setSetId] = useState("");
  const [key, setKey] = useState("");
  const [copies, setCopies] = useState(2);
  const [busy, setBusy] = useState(false);
  const [browse, setBrowse] = useState(false);
  return <><form className="protected-form panel" onSubmit={(event) => { event.preventDefault(); setBusy(true); importProtectedFolder(token, { name, source_path: sourcePath, mount_path: mountPath, recovery_key: key, required_copies: copies, protected_folder_id: setId.trim() || undefined }).then((next) => onComplete(next.folder.id)).catch((err) => onError(err.message)).finally(() => setBusy(false)); }}><div className="section-heading"><div><Download /><h2>Fresh-Mac recovery import</h2></div><span>Either independent disk works</span></div><p>Connect an enrolled disk. The catalog is rebuilt from tagged Restic snapshots without the original SQLite database.</p><div className="field-group"><label htmlFor="import-name">Display name</label><input id="import-name" value={name} onChange={(event) => setName(event.target.value)} /></div><div className="field-group"><label htmlFor="import-source">Future restore path</label><div className="input-with-button"><input id="import-source" value={sourcePath} onChange={(event) => setSourcePath(event.target.value)} required /><button type="button" onClick={() => setBrowse(true)}><FolderOpen />Browse</button></div></div><div className="field-group"><label htmlFor="import-volume">Connected enrolled disk</label><select id="import-volume" value={mountPath} onChange={(event) => setMountPath(event.target.value)} required><option value="">Select a disk</option>{volumes.map((volume) => <option value={volume.mount_path} key={volume.volume_uuid}>{volume.name} · {formatBytes(volume.free_bytes)} free</option>)}</select></div><div className="field-group"><label htmlFor="import-copies">Required independent copies</label><input id="import-copies" type="number" min="1" max="10" value={copies} onChange={(event) => setCopies(Number(event.target.value))} /></div><details className="advanced-options field-span-2"><summary>Multiple protected sets on one disk</summary><label htmlFor="import-set">Protected set ID</label><input id="import-set" value={setId} onChange={(event) => setSetId(event.target.value)} /></details><div className="field-group field-span-2"><label htmlFor="import-key">Recovery key</label><input id="import-key" type="password" value={key} onChange={(event) => setKey(event.target.value)} required /></div><button disabled={busy || !ready} title={ready ? "" : blockedReason || "Checking prerequisites"}>{busy ? <Loader2 className="spin" /> : <Download />}Import catalog</button></form>{browse && <FolderBrowser token={token} initialPath={sourcePath} title="Choose the future restore folder" onClose={() => setBrowse(false)} onSelect={(next) => { setSourcePath(next); setBrowse(false); }} />}</>;
}

function RestorePanel({ token, selectedCount, connectedDrives, busy, onClose, onRestore }: { token: string; selectedCount: number; connectedDrives: DriveTarget[]; busy: boolean; onClose: () => void; onRestore: (driveId?: string, destination?: string) => void }) {
  const [driveId, setDriveId] = useState("");
  const [mode, setMode] = useState<"original" | "alternate">("original");
  const [destination, setDestination] = useState("");
  const [browse, setBrowse] = useState(false);
  return <><section className="restore-panel"><div className="section-heading"><div><RotateCcw /><h2>Restore {selectedCount} selected file{selectedCount === 1 ? "" : "s"}</h2></div><button onClick={onClose}>Close</button></div><p>The application never overwrites an existing path. Leave the drive on Automatic to use any healthy connected replica.</p>{selectedCount === 0 && <p className="muted">Select one or more catalog files before starting a restore.</p>}<div className="form-grid"><div className="field-group"><label htmlFor="restore-drive">Source replica</label><select id="restore-drive" value={driveId} onChange={(event) => setDriveId(event.target.value)}><option value="">Automatic healthy replica</option>{connectedDrives.map((drive) => <option value={drive.id} key={drive.id}>{drive.volume_name}</option>)}</select></div><div className="field-group"><label>Destination</label><div className="segmented-control"><button className={mode === "original" ? "active" : ""} onClick={() => setMode("original")}>Original path</button><button className={mode === "alternate" ? "active" : ""} onClick={() => setMode("alternate")}>Another folder</button></div></div>{mode === "alternate" && <div className="field-group field-span-2"><label htmlFor="restore-destination">Alternate destination</label><div className="input-with-button"><input id="restore-destination" value={destination} onChange={(event) => setDestination(event.target.value)} /><button onClick={() => setBrowse(true)}><FolderOpen />Browse</button></div></div>}</div><button className="primary-inline" disabled={busy || selectedCount === 0 || (mode === "alternate" && !destination)} onClick={() => onRestore(driveId || undefined, mode === "alternate" ? destination : undefined)}><Upload />Start safe restore</button></section>{browse && <FolderBrowser token={token} initialPath={destination} title="Choose an alternate restore folder" onClose={() => setBrowse(false)} onSelect={(next) => { setDestination(next); setBrowse(false); }} />}</>;
}

function OperationHistory({ operations }: { operations: ProtectionOperation[] }) {
  return <section className="panel operation-history"><div className="section-heading"><div><History /><h2>Operation history</h2></div><span>{operations.length} recent</span></div>{operations.length === 0 ? <p className="muted">No protected-folder operations have run yet.</p> : <div>{operations.map((operation) => <article key={operation.id}><span className={`status-pill ${operation.status}`}>{operation.status}</span><div><strong>{humanize(operation.kind)}</strong><small>{formatDate(operation.created_at)}</small><p>{operation.error || operation.progress_message}</p></div><details><summary>Details</summary>{operation.result && <pre>{JSON.stringify(operation.result, null, 2)}</pre>}{Object.keys(operation.diagnostics || {}).length > 0 && <pre>{JSON.stringify(operation.diagnostics, null, 2)}</pre>}</details></article>)}</div>}</section>;
}

function ReplicaBadge({ status }: { status: string }) {
  const labels: Record<string, string> = { structural_checked: "structural", full_checked: "full", missing: "missing", suspect: "suspect", backed_up: "unchecked" };
  return <span className={`replica-badge ${status}`} title={replicaExplanation(status)}>{labels[status] || status}</span>;
}

function replicaExplanation(status: string) {
  if (status === "structural_checked") return "Repository structure checked";
  if (status === "full_checked") return "Stored data blocks read back";
  if (status === "suspect") return "Replica needs attention";
  if (status === "backed_up") return "Backed up but not yet verified";
  return "No verified replica for this file version";
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value)) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${amount.toFixed(amount >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatDate(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : "Never";
}

function formatEtaDuration(value: number): string {
  const totalSeconds = Math.max(0, Math.ceil(value));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

function shortPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts.slice(-2).join("/") || path;
}

function humanize(value: string): string {
  return value.replace(/_/g, " ").replace(/^./, (letter) => letter.toUpperCase());
}
