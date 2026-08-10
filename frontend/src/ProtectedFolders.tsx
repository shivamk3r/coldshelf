import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  HardDrive,
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Upload
} from "lucide-react";

import {
  cancelProtectionOperation,
  confirmRecoveryKey,
  createProtectedFolder,
  createProtectionOperation,
  enrollProtectedDrive,
  getProtectedFolder,
  importProtectedFolder,
  listMountedVolumes,
  listProtectedFolders,
  openProtectionEvents
} from "./api";
import type { ProtectionOperationPayload } from "./api";
import type {
  DriveTarget,
  MountedVolume,
  ProtectedFolder,
  ProtectedFolderCreated,
  ProtectedFolderDetail,
  ProtectionOperation
} from "./types";

export function ProtectedFolders({ token }: { token: string }) {
  const [folders, setFolders] = useState<ProtectedFolder[]>([]);
  const [activeId, setActiveId] = useState("");
  const [detail, setDetail] = useState<ProtectedFolderDetail | null>(null);
  const [volumes, setVolumes] = useState<MountedVolume[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [operation, setOperation] = useState<ProtectionOperation | null>(null);
  const [created, setCreated] = useState<ProtectedFolderCreated | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [evictionPreview, setEvictionPreview] = useState<Record<string, unknown> | null>(null);
  const [evictionPhrase, setEvictionPhrase] = useState("");
  const [previewAll, setPreviewAll] = useState(false);
  const [previewFileIds, setPreviewFileIds] = useState<string[]>([]);
  const [recoveryPaste, setRecoveryPaste] = useState("");
  const sourceRef = useRef<EventSource | null>(null);

  async function refresh(folderId = activeId, search = query) {
    const [nextFolders, mounted] = await Promise.all([
      listProtectedFolders(token),
      listMountedVolumes(token)
    ]);
    setFolders(nextFolders);
    setVolumes(mounted.volumes);
    const nextId = folderId || nextFolders[0]?.id || "";
    if (nextId) {
      setActiveId(nextId);
      setDetail(await getProtectedFolder(token, nextId, search));
    } else {
      setDetail(null);
    }
  }

  useEffect(() => {
    refresh().catch((err) => setError(err.message));
    const interval = window.setInterval(() => {
      refresh().catch((err) => setError(err.message));
    }, 5000);
    return () => {
      window.clearInterval(interval);
      sourceRef.current?.close();
    };
  }, [token, activeId, query]);

  async function selectFolder(folderId: string) {
    setActiveId(folderId);
    setSelected(new Set());
    setEvictionPreview(null);
    setError("");
    setDetail(await getProtectedFolder(token, folderId));
  }

  async function runOperation(payload: ProtectionOperationPayload) {
    if (!activeId) return;
    setError("");
    setNotice("");
    setEvictionPreview(null);
    const queued = await createProtectionOperation(token, activeId, payload);
    setOperation(queued);
    sourceRef.current?.close();
    const source = openProtectionEvents(token, queued.id, (next) => {
      setOperation(next);
      if (["completed", "failed", "cancelled"].includes(next.status)) {
        source.close();
        if (next.status === "completed") {
          setNotice(`${next.kind} completed`);
          if (next.kind === "evict" && next.request.dry_run === true && next.result) {
            setEvictionPreview(next.result);
          } else {
            setSelected(new Set());
          }
          refresh(activeId).catch((err) => setError(err.message));
        } else {
          setError(next.error || `${next.kind} ${next.status}`);
        }
      }
    });
    source.onerror = () => source.close();
    sourceRef.current = source;
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

  const busy = operation?.status === "queued" || operation?.status === "running";
  const connectedDrives = detail?.drives.filter((drive) => drive.mounted) || [];
  const enrolledUuids = new Set(detail?.drives.map((drive) => drive.volume_uuid) || []);
  const enrollable = volumes.filter((volume) => !enrolledUuids.has(volume.volume_uuid));
  const pendingPrompt =
    detail && connectedDrives.length > 0 && detail.summary.pending_files > 0;

  return (
    <section className="protected-dashboard">
      {error && <div className="alert">{error}</div>}
      {notice && <div className="result-toast"><CheckCircle2 />{notice}</div>}

      <header className="protected-header">
        <div>
          <span className="eyebrow">Independent encrypted archives</span>
          <h2>Protected folders</h2>
          <p>Catalog files offline, back up one disk at a time, and evict only at full coverage.</p>
        </div>
        <div className="button-row">
          <button onClick={() => setShowImport((value) => !value)}><Download />Recover</button>
          <button className="primary-inline" onClick={() => setShowCreate((value) => !value)}>
            <Plus />New folder
          </button>
        </div>
      </header>

      {showCreate && (
        <CreateFolderForm
          token={token}
          created={created}
          setCreated={setCreated}
          onComplete={() => {
            setShowCreate(false);
            refresh().catch((err) => setError(err.message));
          }}
          onError={setError}
        />
      )}

      {showImport && (
        <RecoveryImportForm
          token={token}
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
          {folders.length === 0 && <p className="muted">No protected folders yet.</p>}
          {folders.map((folder) => (
            <button
              key={folder.id}
              className={folder.id === activeId ? "active" : ""}
              onClick={() => selectFolder(folder.id).catch((err) => setError(err.message))}
            >
              <ShieldCheck />
              <span>{folder.name}<small>{shortPath(folder.source_path)}</small></span>
            </button>
          ))}
        </nav>

        <div className="protected-main">
          {!detail && <div className="empty-state panel"><ShieldCheck /><h2>Create or recover a protected folder</h2></div>}
          {detail && (
            <>
              {!detail.folder.recovery_confirmed && (
                <div className="alert recovery-confirm-inline">
                  <span>Recovery key confirmation is required before enrolling a disk.</span>
                  <input
                    aria-label="Recovery key confirmation"
                    type="password"
                    value={recoveryPaste}
                    onChange={(event) => setRecoveryPaste(event.target.value)}
                  />
                  <button
                    disabled={!recoveryPaste}
                    onClick={() => confirmRecoveryKey(token, detail.folder.id, recoveryPaste)
                      .then(() => { setRecoveryPaste(""); return refresh(detail.folder.id); })
                      .catch((err) => setError(err.message))}
                  >Confirm saved key</button>
                </div>
              )}
              {pendingPrompt && (
                <div className="pending-prompt">
                  <AlertTriangle />
                  <span>{detail.summary.pending_files} files still need replica work while a disk is connected.</span>
                </div>
              )}

              <section className="protected-title panel">
                <div>
                  <span className="eyebrow">{detail.folder.required_copies} verified repositories required</span>
                  <h2>{detail.folder.name}</h2>
                  <p>{detail.folder.source_path}</p>
                </div>
                <button disabled={busy} onClick={() => runOperation({ kind: "index" }).catch((err) => setError(err.message))}>
                  <RefreshCw />Index now
                </button>
              </section>

              <SummaryMetrics detail={detail} />

              <section className="drive-grid">
                {detail.drives.map((drive) => (
                  <DriveCard
                    key={drive.id}
                    drive={drive}
                    busy={busy}
                    runOperation={runOperation}
                    onError={setError}
                  />
                ))}
                {enrollable.map((volume) => (
                  <article className="drive-card enroll-card" key={volume.volume_uuid}>
                    <HardDrive />
                    <h3>{volume.name}</h3>
                    <p>{volume.filesystem} · {formatBytes(volume.free_bytes)} free</p>
                    <button
                      disabled={!detail.folder.recovery_confirmed || busy}
                      onClick={() => enroll(volume).catch((err) => setError(err.message))}
                    >Enroll this disk</button>
                  </article>
                ))}
              </section>

              {operation && (
                <section className="operation-panel panel">
                  <div>
                    <strong>{operation.kind}</strong>
                    <span className={`status-pill ${operation.status}`}>{operation.status}</span>
                    <p>{operation.progress_message}</p>
                    {operation.kind === "backup" && busy && operation.eta_seconds != null && (
                      <p className="operation-eta">
                        About {formatEtaDuration(operation.eta_seconds)} of file transfer remaining.
                      </p>
                    )}
                  </div>
                  <div className="progress-wrap"><div style={{ width: `${operation.progress_percent}%` }} /></div>
                  {busy && (
                    <button onClick={() => cancelProtectionOperation(token, operation.id).catch((err) => setError(err.message))}>
                      Cancel safely
                    </button>
                  )}
                </section>
              )}

              <section className="catalog panel">
                <div className="catalog-header">
                  <div><h2>Offline file catalog</h2><span>{detail.files.length} shown</span></div>
                  <form onSubmit={(event) => {
                    event.preventDefault();
                    refresh(activeId, query).catch((err) => setError(err.message));
                  }}>
                    <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search relative path" />
                    <button aria-label="Search catalog"><Search /></button>
                  </form>
                </div>
                <div className="table-toolbar">
                  <button onClick={() => {
                    const allSelected = detail.files.every((file) => selected.has(file.id));
                    setSelected(allSelected ? new Set() : new Set(detail.files.map((file) => file.id)));
                  }}>
                    {detail.files.every((file) => selected.has(file.id)) ? "Clear" : "Select visible"}
                  </button>
                  <span>{selected.size} selected</span>
                </div>
                <div className="catalog-table-wrap">
                  <table>
                    <thead><tr><th /><th>Path</th><th>Local</th><th>Size</th><th>Coverage</th>{detail.drives.map((drive) => <th key={drive.id}>{drive.volume_name}</th>)}</tr></thead>
                    <tbody>
                      {detail.files.map((file) => (
                        <tr key={file.id}>
                          <td><input type="checkbox" checked={selected.has(file.id)} onChange={() => toggleFile(file.id)} aria-label={`Select ${file.relative_path}`} /></td>
                          <td className="path-cell">{file.relative_path}</td>
                          <td>{file.local_state}</td>
                          <td>{formatBytes(file.size)}</td>
                          <td>{file.verified_replica_count}/{detail.folder.required_copies}</td>
                          {detail.drives.map((drive) => {
                            const replica = file.replicas.find((item) => item.drive_target_id === drive.id);
                            return <td key={drive.id}><ReplicaBadge status={replica?.status || "missing"} /></td>;
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="catalog-actions">
                  <button disabled={busy || selected.size === 0} onClick={() => {
                    setPreviewAll(false);
                    setPreviewFileIds([...selected]);
                    runOperation({ kind: "evict", file_ids: [...selected], dry_run: true }).catch((err) => setError(err.message));
                  }}>Preview selected eviction</button>
                  <button disabled={busy || detail.summary.local_files === 0} onClick={() => {
                    setPreviewAll(true);
                    setPreviewFileIds([]);
                    runOperation({ kind: "evict", all_files: true, dry_run: true }).catch((err) => setError(err.message));
                  }}>Preview all local eviction</button>
                  <button disabled={busy || selected.size === 0 || connectedDrives.length === 0} onClick={() => runOperation({ kind: "restore", file_ids: [...selected] }).catch((err) => setError(err.message))}>
                    <Upload />Restore selected
                  </button>
                </div>
              </section>

              {evictionPreview && (
                <section className="eviction-confirm panel">
                  <AlertTriangle />
                  <div>
                    <h2>Structural-check eviction preview</h2>
                    <p>{String(evictionPreview.verification_warning || "Data blocks were not necessarily read back bit-for-bit.")}</p>
                    <p>{String(evictionPreview.eligible_count || 0)} eligible; {Array.isArray(evictionPreview.blocked) ? evictionPreview.blocked.length : 0} blocked.</p>
                    <label htmlFor="eviction-phrase">Type the exact confirmation phrase</label>
                    <input id="eviction-phrase" value={evictionPhrase} onChange={(event) => setEvictionPhrase(event.target.value)} placeholder={String(evictionPreview.confirmation_phrase || "")} />
                    <button
                      className="danger-button"
                      disabled={busy || evictionPhrase !== evictionPreview.confirmation_phrase || Number(evictionPreview.eligible_count || 0) === 0 || (Array.isArray(evictionPreview.blocked) && evictionPreview.blocked.length > 0)}
                      onClick={() => runOperation({
                        kind: "evict",
                        ...(previewAll ? { all_files: true } : { file_ids: previewFileIds }),
                        dry_run: false,
                        confirmation_phrase: evictionPhrase
                      }).catch((err) => setError(err.message))}
                    >Evict now</button>
                  </div>
                </section>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}

function SummaryMetrics({ detail }: { detail: ProtectedFolderDetail }) {
  const summary = detail.summary;
  const metrics = [
    ["Local", summary.local_files, summary.local_bytes],
    ["Pending 0 copies", summary.unprotected_files, summary.unprotected_bytes],
    [`Partial 1/${detail.folder.required_copies}`, summary.partial_files, summary.partial_bytes],
    [`Protected ${detail.folder.required_copies}/${detail.folder.required_copies}`, summary.protected_files, summary.protected_bytes],
    ["Evicted", summary.evicted_files, summary.evicted_bytes],
    ["At risk", summary.at_risk_files, summary.at_risk_bytes]
  ] as const;
  return <section className="protection-metrics">{metrics.map(([label, files, bytes]) => <div className={label === "At risk" && files ? "metric risk" : "metric"} key={label}><span>{label}</span><strong>{files} files</strong><small>{formatBytes(bytes)}</small></div>)}</section>;
}

function DriveCard({
  drive,
  busy,
  runOperation,
  onError
}: {
  drive: DriveTarget;
  busy: boolean;
  runOperation: (payload: ProtectionOperationPayload) => Promise<void>;
  onError: (message: string) => void;
}) {
  return (
    <article className="drive-card">
      <div className="drive-card-title"><HardDrive /><h3>{drive.volume_name}</h3><span className={drive.mounted ? "drive-online" : "drive-offline"}>{drive.mounted ? "mounted" : "offline"}</span></div>
      <dl>
        <dt>Repository</dt><dd title={drive.repository_id}>{drive.repository_id.slice(0, 12)}…</dd>
        <dt>Capacity</dt><dd>{formatBytes(drive.free_bytes)} free / {formatBytes(drive.total_bytes)}</dd>
        <dt>Last backup</dt><dd>{formatDate(drive.last_backup_at)}</dd>
        <dt>Structural</dt><dd>{formatDate(drive.last_structural_check_at)}</dd>
        <dt>Full read</dt><dd>{formatDate(drive.last_full_check_at)}</dd>
      </dl>
      <div className="button-row">
        <button disabled={busy || !drive.mounted} onClick={() => runOperation({ kind: "backup", drive_target_id: drive.id }).catch((err) => onError(err.message))}>Back up</button>
        <button disabled={busy || !drive.mounted} onClick={() => runOperation({ kind: "verify", drive_target_id: drive.id }).catch((err) => onError(err.message))}>Check</button>
        <button disabled={busy || !drive.mounted} onClick={() => runOperation({ kind: "verify", drive_target_id: drive.id, full: true }).catch((err) => onError(err.message))}>Full check</button>
      </div>
    </article>
  );
}

function CreateFolderForm({
  token,
  created,
  setCreated,
  onComplete,
  onError
}: {
  token: string;
  created: ProtectedFolderCreated | null;
  setCreated: (folder: ProtectedFolderCreated | null) => void;
  onComplete: () => void;
  onError: (message: string) => void;
}) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [copies, setCopies] = useState(2);
  const [confirmation, setConfirmation] = useState("");
  if (created) {
    return (
      <section className="recovery-key-panel panel">
        <KeyRound />
        <div><h2>Save this recovery key now</h2><p>Store it in your password manager. It will not be shown by the app again.</p><code>{created.recovery_key}</code><pre>{created.recovery_instructions}</pre><label htmlFor="recovery-confirm">Paste the key to confirm</label><input id="recovery-confirm" type="password" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /><button disabled={confirmation !== created.recovery_key} onClick={() => confirmRecoveryKey(token, created.id, confirmation).then(() => { setCreated(null); onComplete(); }).catch((err) => onError(err.message))}>Confirm recovery key</button></div>
      </section>
    );
  }
  return (
    <form className="protected-form panel" onSubmit={(event) => {
      event.preventDefault();
      createProtectedFolder(token, { name, source_path: path, required_copies: copies }).then(setCreated).catch((err) => onError(err.message));
    }}>
      <h2>Create protected folder</h2><label htmlFor="protected-name">Name</label><input id="protected-name" value={name} onChange={(event) => setName(event.target.value)} required /><label htmlFor="protected-path">Mac folder path</label><input id="protected-path" value={path} onChange={(event) => setPath(event.target.value)} required placeholder="/Users/you/large-files" /><label htmlFor="protected-copies">Required verified copies</label><input id="protected-copies" type="number" min="1" max="10" value={copies} onChange={(event) => setCopies(Number(event.target.value))} /><button className="primary-inline"><Plus />Create and generate key</button>
    </form>
  );
}

function RecoveryImportForm({ token, volumes, onComplete, onError }: { token: string; volumes: MountedVolume[]; onComplete: (folderId: string) => void; onError: (message: string) => void }) {
  const [name, setName] = useState("Recovered protected folder");
  const [sourcePath, setSourcePath] = useState("");
  const [mountPath, setMountPath] = useState(volumes[0]?.mount_path || "");
  const [setId, setSetId] = useState("");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <form
      className="protected-form panel"
      onSubmit={(event) => {
        event.preventDefault();
        setBusy(true);
        importProtectedFolder(token, {
          name,
          source_path: sourcePath,
          mount_path: mountPath,
          recovery_key: key,
          required_copies: 2,
          protected_folder_id: setId.trim() || undefined
        })
          .then((detail) => onComplete(detail.folder.id))
          .catch((err) => onError(err.message))
          .finally(() => setBusy(false));
      }}
    >
      <h2>Fresh-Mac recovery import</h2>
      <p>Connect either disk. The catalog is rebuilt from its Restic snapshots.</p>
      <label htmlFor="import-name">Name</label>
      <input id="import-name" value={name} onChange={(event) => setName(event.target.value)} />
      <label htmlFor="import-source">Future restore path</label>
      <input id="import-source" value={sourcePath} onChange={(event) => setSourcePath(event.target.value)} required />
      <label htmlFor="import-volume">Connected disk</label>
      <select id="import-volume" value={mountPath} onChange={(event) => setMountPath(event.target.value)} required>
        <option value="">Select a disk</option>
        {volumes.map((volume) => <option value={volume.mount_path} key={volume.volume_uuid}>{volume.name}</option>)}
      </select>
      <label htmlFor="import-set">Protected set ID (only needed when this disk has multiple sets)</label>
      <input id="import-set" value={setId} onChange={(event) => setSetId(event.target.value)} />
      <label htmlFor="import-key">Recovery key</label>
      <input id="import-key" type="password" value={key} onChange={(event) => setKey(event.target.value)} required />
      <button disabled={busy}>{busy ? <Loader2 className="spin" /> : <Download />}Import catalog</button>
    </form>
  );
}

function ReplicaBadge({ status }: { status: string }) {
  const label = status === "structural_checked" ? "structural" : status === "full_checked" ? "full" : status;
  return <span className={`replica-badge ${status}`}>{label}</span>;
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value)) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${amount.toFixed(1)} ${units[unit]}`;
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
  const parts = path.split("/");
  return parts.slice(-2).join("/") || path;
}
