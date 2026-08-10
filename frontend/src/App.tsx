import { useEffect, useState } from "react";
import {
  Archive,
  BarChart3,
  BookOpen,
  CircleHelp,
  CopyCheck,
  Database,
  FileSearch,
  HardDrive,
  Home,
  KeyRound,
  Loader2,
  LogOut,
  RotateCcw,
  ShieldCheck,
  Trash2
} from "lucide-react";

import { ApiError, clearToken, getConfig, initialToken, saveToken } from "./api";
import { HelpCenter } from "./HelpCenter";
import { ProtectedFolders } from "./ProtectedFolders";
import { ReportsDashboard } from "./ReportsDashboard";
import type { ReportIntent } from "./ReportsDashboard";

type View = "home" | "reports" | "protection" | "help";
export type ProtectionIntent = "manage" | "create" | "restore" | "recover";

export function App() {
  const [token, setToken] = useState(initialToken());
  const [tokenInput, setTokenInput] = useState("");
  const [view, setView] = useState<View>(initialView());
  const [reportIntent, setReportIntent] = useState<ReportIntent>("analyze");
  const [protectionIntent, setProtectionIntent] = useState<ProtectionIntent>("manage");
  const [helpArticle, setHelpArticle] = useState("start");
  const [jobsCount, setJobsCount] = useState(0);
  const [checkingToken, setCheckingToken] = useState(Boolean(token));
  const [authError, setAuthError] = useState("");

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

  function openProtection(intent: ProtectionIntent) {
    setProtectionIntent(intent);
    setView("protection");
  }

  function openHelp(article = "start") {
    setHelpArticle(article);
    setView("help");
  }

  function disconnect() {
    clearToken();
    setToken("");
    setTokenInput("");
    setAuthError("");
  }

  if (!token || checkingToken) {
    return (
      <main className="auth-screen">
        <section className="auth-panel">
          <div className="auth-mark"><HardDrive aria-hidden="true" /></div>
          <span className="eyebrow">Local disk maintenance</span>
          <h1>Disk Space Manager</h1>
          {checkingToken ? (
            <div className="loading-inline"><Loader2 className="spin" />Connecting to the local dashboard…</div>
          ) : (
            <>
              <p>Start the server, then use its private token to connect:</p>
              <code className="startup-command">uv run disk-space-manager-web</code>
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
          <div className="brand-icon"><HardDrive aria-hidden="true" /></div>
          <div><h1>Disk Space Manager</h1><span>Local dashboard</span></div>
        </div>
        <nav className="primary-nav" aria-label="Dashboard">
          <NavButton active={view === "home"} icon={<Home />} label="Home" onClick={() => setView("home")} />
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
            jobsCount={jobsCount}
            openReports={openReports}
            openProtection={openProtection}
            openHelp={openHelp}
          />
        )}
        {view === "reports" && (
          <ReportsDashboard
            token={token}
            intent={reportIntent}
            onOpenHelp={openHelp}
            onJobsChanged={setJobsCount}
          />
        )}
        {view === "protection" && (
          <ProtectedFolders token={token} intent={protectionIntent} onOpenHelp={openHelp} />
        )}
        {view === "help" && (
          <HelpCenter
            initialArticle={helpArticle}
            onOpenReports={() => openReports("analyze")}
            onOpenProtection={() => openProtection("manage")}
          />
        )}
      </section>
    </main>
  );
}

function HomeDashboard({ jobsCount, openReports, openProtection, openHelp }: {
  jobsCount: number;
  openReports: (intent: ReportIntent) => void;
  openProtection: (intent: ProtectionIntent) => void;
  openHelp: (article?: string) => void;
}) {
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
          <span>Dry-run previews</span>
          <span>Exact confirmation phrases</span>
          <span>Persistent action logs</span>
        </div>
      </header>

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

function initialView(): View {
  const value = window.location.hash.replace(/^#\//, "");
  return ["reports", "protection", "help"].includes(value) ? value as View : "home";
}
