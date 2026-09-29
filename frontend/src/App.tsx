import { useCallback, useEffect, useState } from "react";
import {
  Archive,
  BarChart3,
  BookOpen,
  CircleHelp,
  CopyCheck,
  Database,
  FileSearch,
  FolderTree,
  HardDrive,
  Home,
  KeyRound,
  Loader2,
  LogOut,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Trash2
} from "lucide-react";

import { ApiError, clearToken, getConfig, getStorageSummary, initialToken, saveToken } from "./api";
import { BrowseDashboard, clearBrowseCache } from "./BrowseDashboard";
import type { BrowseProtectionSeed } from "./BrowseDashboard";
import { BrowseDeletionToast, useBrowseDeletionController } from "./BrowseDeletionToast";
import { HelpCenter } from "./HelpCenter";
import { ProtectedFolders } from "./ProtectedFolders";
import { ReportsDashboard } from "./ReportsDashboard";
import { StorageDashboard } from "./StorageDashboard";
import type { ReportIntent } from "./ReportsDashboard";
import type { StorageSummary } from "./types";

type View = "home" | "storage" | "browse" | "reports" | "protection" | "help";
export type ProtectionIntent = "manage" | "create" | "restore" | "recover";

interface ProtectionLaunch {
  seed?: BrowseProtectionSeed;
  folderId?: string;
  returnPath?: string;
}

export function App() {
  const [token, setToken] = useState(initialToken());
  const [tokenInput, setTokenInput] = useState("");
  const [view, setView] = useState<View>(initialView());
  const [reportIntent, setReportIntent] = useState<ReportIntent>("analyze");
  const [protectionIntent, setProtectionIntent] = useState<ProtectionIntent>("manage");
  const [protectionLaunch, setProtectionLaunch] = useState<ProtectionLaunch>({});
  const [browsePath, setBrowsePath] = useState("");
  const [helpArticle, setHelpArticle] = useState("start");
  const [jobsCount, setJobsCount] = useState(0);
  const [checkingToken, setCheckingToken] = useState(Boolean(token));
  const [authError, setAuthError] = useState("");
  const [browseRefreshVersion, setBrowseRefreshVersion] = useState(0);
  const onDeletionFilesystemChanged = useCallback(() => {
    clearBrowseCache();
    setBrowseRefreshVersion((version) => version + 1);
  }, []);
  const deletionController = useBrowseDeletionController(
    token,
    onDeletionFilesystemChanged
  );

  useEffect(() => {
    if (!token) {
      setCheckingToken(false);
      return;
    }
    saveToken(token);
    setCheckingToken(true);
    getConfig(token)
      .then(() => setAuthError(""))
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) {
          clearToken();
          setToken("");
          setAuthError("That token was not accepted. Reopen the URL printed by the server or enter its token below.");
        } else {
          setAuthError(err instanceof Error ? err.message : "The dashboard could not connect to the server.");
        }
      })
      .finally(() => setCheckingToken(false));
  }, [token]);

  useEffect(() => {
    const hash = view === "home" ? "" : `#/${view}`;
    window.history.replaceState({}, "", `${window.location.pathname}${window.location.search}${hash}`);
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
  }, [view]);

  function openReports(intent: ReportIntent) {
    setReportIntent(intent);
    setView("reports");
  }

  function openProtection(intent: ProtectionIntent, launch: ProtectionLaunch = {}) {
    setProtectionIntent(intent);
    setProtectionLaunch(launch);
    setView("protection");
  }

  function openBrowse(path = browsePath) {
    if (path) setBrowsePath(path);
    setView("browse");
  }

  function openHelp(article = "start") {
    setHelpArticle(article);
    setView("help");
  }

  function disconnect() {
    clearToken();
    clearBrowseCache();
    setToken("");
    setTokenInput("");
    setAuthError("");
  }

  if (!token || checkingToken) {
    return (
      <main className="auth-screen">
        <section className="auth-panel">
          <img className="auth-mark" src="/snowflake.svg" alt="" />
          <span className="eyebrow">Local disk maintenance</span>
          <h1>coldshelf</h1>
          {checkingToken ? (
            <div className="loading-inline"><Loader2 className="spin" />Connecting to the local dashboard…</div>
          ) : (
            <>
              <p>Start the server, then use its private token to connect:</p>
              <code className="startup-command">coldshelf web</code>
              {authError && <div className="alert" role="alert">{authError}</div>}
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  const next = tokenInput.trim();
                  if (next) setToken(next);
                }}
              >
                <label htmlFor="token">API token</label>
                <input id="token" type="password" value={tokenInput} onChange={(event) => setTokenInput(event.target.value)} autoFocus />
                <button type="submit" className="primary-inline">Connect</button>
              </form>
              <p className="auth-note"><KeyRound />The token authorizes reports and confirmed actions on this computer. Keep it private.</p>
            </>
          )}
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand-row">
          <img className="brand-icon" src="/snowflake.svg" alt="" />
          <div><h1>coldshelf</h1><span>Local dashboard</span></div>
        </div>
        <nav className="primary-nav" aria-label="Dashboard">
          <NavButton active={view === "home"} icon={<Home />} label="Home" onClick={() => setView("home")} />
          <NavButton active={view === "storage"} icon={<HardDrive />} label="Storage" onClick={() => setView("storage")} />
          <NavButton active={view === "browse"} icon={<FolderTree />} label="Browse" onClick={() => openBrowse()} />
          <NavButton active={view === "reports"} icon={<Database />} label="Reports" badge={jobsCount || undefined} onClick={() => setView("reports")} />
          <NavButton active={view === "protection"} icon={<ShieldCheck />} label="Protected folders" onClick={() => openProtection("manage")} />
          <NavButton active={view === "help"} icon={<BookOpen />} label="Help" onClick={() => openHelp()} />
        </nav>
        <div className="sidebar-guide">
          <CircleHelp />
          <strong>Not sure what to do?</strong>
          <p>Start from Home or open the safety guides before changing files.</p>
          <button onClick={() => openHelp()}>Open help center</button>
        </div>
        <footer className="sidebar-footer">
          <span><i />Connected locally</span>
          <button onClick={disconnect}><LogOut />Disconnect</button>
        </footer>
      </aside>

      <section className="content">
        {view === "home" && (
          <HomeDashboard
            token={token}
            jobsCount={jobsCount}
            openStorage={() => setView("storage")}
            openReports={openReports}
            openProtection={openProtection}
            openHelp={openHelp}
          />
        )}
        {view === "storage" && <StorageDashboard token={token} onOpenBrowse={openBrowse} />}
        {view === "reports" && (
          <ReportsDashboard
            token={token}
            intent={reportIntent}
            onOpenHelp={openHelp}
            onJobsChanged={setJobsCount}
            onFilesystemChanged={clearBrowseCache}
          />
        )}
        {view === "browse" && (
          <BrowseDashboard
            token={token}
            initialPath={browsePath}
            onPathChanged={setBrowsePath}
            onProtect={(seed) => openProtection("create", { seed, returnPath: seed.returnPath })}
            onOpenProtected={(folderId, returnPath) => openProtection("manage", { folderId, returnPath })}
            onOpenHelp={openHelp}
            activeDeletion={deletionController.deletion}
            refreshVersion={browseRefreshVersion}
            onDeletionScheduled={deletionController.onScheduled}
          />
        )}
        {view === "protection" && (
          <ProtectedFolders
            token={token}
            intent={protectionIntent}
            createDefaults={protectionLaunch.seed}
            targetFolderId={protectionLaunch.folderId}
            returnPath={protectionLaunch.returnPath}
            onReturnToBrowse={(path) => openBrowse(path)}
            onFilesystemChanged={clearBrowseCache}
            onOpenHelp={openHelp}
          />
        )}
        {view === "help" && (
          <HelpCenter
            initialArticle={helpArticle}
            onOpenReports={() => openReports("analyze")}
            onOpenProtection={() => openProtection("manage")}
            onOpenBrowse={() => openBrowse()}
          />
        )}
      </section>
      <BrowseDeletionToast
        deletion={deletionController.deletion}
        error={deletionController.error}
        undoing={deletionController.undoing}
        onUndo={() => void deletionController.undo()}
        onDismiss={deletionController.dismiss}
      />
    </main>
  );
}

function HomeDashboard({ token, jobsCount, openStorage, openReports, openProtection, openHelp }: {
  token: string;
  jobsCount: number;
  openStorage: () => void;
  openReports: (intent: ReportIntent) => void;
  openProtection: (intent: ProtectionIntent) => void;
  openHelp: (article?: string) => void;
}) {
  const [storage, setStorage] = useState<StorageSummary | null>(null);
  const [storageLoading, setStorageLoading] = useState(true);
  const [storageError, setStorageError] = useState("");

  const loadStorage = useCallback(async () => {
    setStorageLoading(true);
    setStorageError("");
    try {
      setStorage(await getStorageSummary(token));
    } catch (err) {
      setStorageError(err instanceof Error ? err.message : "Storage information is temporarily unavailable.");
    } finally {
      setStorageLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void loadStorage();
  }, [loadStorage]);

  const usedPercent = storage && storage.total_bytes > 0
    ? Math.min(100, Math.max(0, (storage.used_bytes / storage.total_bytes) * 100))
    : 0;

  return (
    <section className="home-dashboard">
      <header className="hero-panel">
        <div>
          <span className="eyebrow">Choose an outcome</span>
          <h2>What would you like to do?</h2>
          <p>Every file-changing workflow starts with a preview and ends with an explicit confirmation.</p>
          <div className="button-row">
            <button className="primary-inline" onClick={() => openReports("analyze")}><FileSearch />Start a safe scan</button>
            <button onClick={() => openHelp("start")}><BookOpen />Read the quick start</button>
          </div>
        </div>
        <div className="safety-card">
          <ShieldCheck />
          <strong>Safety is built in</strong>
          <span>Server-validated previews</span>
          <span>Explicit confirmations</span>
          <span>Persistent action logs</span>
        </div>
      </header>

      <section className="storage-panel panel" aria-labelledby="primary-storage-title">
        <header className="storage-panel-header">
          <div className="storage-title">
            <span className="storage-icon"><HardDrive aria-hidden="true" /></span>
            <div><span className="eyebrow">This computer</span><h3 id="primary-storage-title">Primary storage</h3></div>
          </div>
          <button
            type="button"
            disabled={storageLoading}
            aria-label={storageError ? "Retry storage summary" : "Refresh storage summary"}
            onClick={() => void loadStorage()}
          >
            <RefreshCw className={storageLoading ? "spin" : undefined} />
            {storageLoading ? "Refreshing…" : storageError ? "Retry" : "Refresh"}
          </button>
        </header>

        {storageLoading ? (
          <div className="storage-state" role="status"><Loader2 className="spin" /><span>Reading primary storage…</span></div>
        ) : storageError ? (
          <div className="storage-state storage-error" role="alert">
            <strong>Storage information is unavailable.</strong>
            <span>{storageError}</span>
          </div>
        ) : storage ? (
          <div className="storage-content">
            <div className="storage-highlight">
              <span>Available</span>
              <strong>{formatBytes(storage.available_bytes)}</strong>
              <small>on the primary disk</small>
            </div>
            <div className="storage-usage">
              <div className="storage-meter-copy">
                <span>{formatBytes(storage.used_bytes)} of {formatBytes(storage.total_bytes)} used</span>
                <strong>{Math.round(usedPercent)}% used</strong>
              </div>
              <div
                className="storage-meter"
                role="progressbar"
                aria-label="Primary storage used"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(usedPercent)}
                aria-valuetext={`${formatBytes(storage.used_bytes)} of ${formatBytes(storage.total_bytes)} used`}
              >
                <span style={{ width: `${usedPercent}%` }} />
              </div>
              <dl className="storage-metrics">
                <div><dt>Total</dt><dd>{formatBytes(storage.total_bytes)}</dd></div>
                <div><dt>Used</dt><dd>{formatBytes(storage.used_bytes)}</dd></div>
                <div><dt>Available</dt><dd>{formatBytes(storage.available_bytes)}</dd></div>
              </dl>
            </div>
            <div className="storage-actions">
              <button type="button" onClick={openStorage}><HardDrive />Where is space used?</button>
              <button type="button" className="primary-inline" onClick={() => openReports("analyze")}><BarChart3 />Analyze storage</button>
              <button type="button" onClick={() => openReports("archive")}><Archive />Archive old files</button>
            </div>
          </div>
        ) : null}
      </section>

      <div className="home-section-heading">
        <div><span className="eyebrow">Disk reports</span><h3>Understand and free space</h3></div>
        {jobsCount > 0 && <button onClick={() => openReports("analyze")}>{jobsCount} saved report{jobsCount === 1 ? "" : "s"}</button>}
      </div>
      <section className="task-grid">
        <TaskCard icon={<BarChart3 />} tone="teal" title="Analyze storage" description="See total usage, file types, largest folders, and scan errors." safety="Read-only" onClick={() => openReports("analyze")} />
        <TaskCard icon={<Trash2 />} tone="rose" title="Clean cache files" description="Find cache-like files, choose exactly what to remove, and preview first." safety="Preview + confirm" onClick={() => openReports("clean")} />
        <TaskCard icon={<Archive />} tone="amber" title="Archive old files" description="Move selected old files to a drive or folder and leave symlinks behind." safety="Preview + confirm" onClick={() => openReports("archive")} />
        <TaskCard icon={<CopyCheck />} tone="blue" title="Review duplicates" description="Find exact and near-duplicate groups for manual review." safety="Advisory only" onClick={() => openReports("duplicates")} />
      </section>

      <div className="home-section-heading"><div><span className="eyebrow">Resilient storage</span><h3>Protect and recover important folders</h3></div><button onClick={() => openHelp("protect-setup")}>Read the protection guide</button></div>
      <section className="task-grid protection-tasks">
        <TaskCard icon={<ShieldCheck />} tone="green" title="Protect a folder" description="Create encrypted independent replicas on external disks." safety="macOS + Restic" onClick={() => openProtection("create")} />
        <TaskCard icon={<RotateCcw />} tone="purple" title="Restore files" description="Restore selected catalog files from any healthy connected replica." safety="Never overwrites" onClick={() => openProtection("restore")} />
        <TaskCard icon={<KeyRound />} tone="slate" title="Recover a catalog" description="Rebuild protected-folder history on a fresh Mac from either disk." safety="Recovery key required" onClick={() => openProtection("recover")} />
      </section>
    </section>
  );
}

function TaskCard({ icon, tone, title, description, safety, onClick }: {
  icon: React.ReactNode;
  tone: string;
  title: string;
  description: string;
  safety: string;
  onClick: () => void;
}) {
  return (
    <button className="task-card" onClick={onClick}>
      <span className={`task-icon ${tone}`}>{icon}</span>
      <span className="task-copy"><strong>{title}</strong><small>{description}</small></span>
      <span className="task-safety">{safety}</span>
    </button>
  );
}

function NavButton({ active, icon, label, badge, onClick }: {
  active: boolean;
  icon: React.ReactNode;
  label: string;
  badge?: number;
  onClick: () => void;
}) {
  return <button className={active ? "active" : ""} aria-current={active ? "page" : undefined} onClick={onClick}>{icon}<span>{label}</span>{badge ? <small>{badge}</small> : null}</button>;
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  return `${amount.toFixed(amount >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function initialView(): View {
  const value = window.location.hash.replace(/^#\//, "");
  return ["storage", "browse", "reports", "protection", "help"].includes(value) ? value as View : "home";
}
