import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  AlertTriangle,
  Archive,
  BarChart3,
  CheckCircle2,
  ChevronRight,
  CopyCheck,
  FileSearch,
  FolderOpen,
  FolderSearch,
  HardDrive,
  History,
  Info,
  Loader2,
  RefreshCw,
  Search,
  Trash2
} from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from "recharts";

import {
  createJob,
  getConfig,
  getReport,
  listArchiveTargets,
  listJobActions,
  listJobs,
  openJobEvents,
  runArchive,
  runClean
} from "./api";
import { FolderBrowser } from "./FolderBrowser";
import type { ActionResult, ArchiveTarget, Candidate, DuplicateGroup, Job, Report } from "./types";

export type ReportIntent = "analyze" | "clean" | "archive" | "duplicates" | "full";
type ReportTab = "overview" | "large" | "duplicates" | "clean" | "archive" | "activity";

const CHART_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)"
];
const CHART_AXIS_PROPS = {
  axisLine: { stroke: "var(--control-border)" },
  tick: { fill: "var(--muted)", fontSize: 12 },
  tickLine: { stroke: "var(--control-border)" }
};
const CHART_TOOLTIP_PROPS = {
  contentStyle: {
    background: "var(--surface)",
    border: "1px solid var(--control-border)",
    borderRadius: "8px",
    boxShadow: "var(--shadow)",
    color: "var(--ink)"
  },
  itemStyle: { color: "var(--ink)" },
  labelStyle: { color: "var(--ink)" }
};
const PAGE_SIZE = 25;

const INTENT_COPY: Record<ReportIntent, { title: string; description: string; tab: ReportTab }> = {
  analyze: {
    title: "Analyze storage",
    description: "Create a read-only overview of file sizes and largest items.",
    tab: "overview"
  },
  clean: {
    title: "Find cache files",
    description: "Scan first, then review cache candidates before any deletion.",
    tab: "clean"
  },
  archive: {
    title: "Find old files to archive",
    description: "Use the age threshold to find large, older files and choose their destination.",
    tab: "archive"
  },
  duplicates: {
    title: "Review duplicates",
    description: "Find exact matches and optional near-duplicate media groups without changing files.",
    tab: "duplicates"
  },
  full: {
    title: "Create a full report",
    description: "Run storage, cleanup, old-file, and duplicate analysis together.",
    tab: "overview"
  }
};

export function ReportsDashboard({
  token,
  intent,
  onOpenHelp,
  onJobsChanged
}: {
  token: string;
  intent: ReportIntent;
  onOpenHelp: (article?: string) => void;
  onJobsChanged: (count: number) => void;
}) {
  const [scanPath, setScanPath] = useState("");
  const [ageMonths, setAgeMonths] = useState(6);
  const [includeDuplicates, setIncludeDuplicates] = useState(intent === "duplicates" || intent === "full");
  const [includeNearDuplicates, setIncludeNearDuplicates] = useState(intent === "duplicates" || intent === "full");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [activeJob, setActiveJob] = useState<Job | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [actions, setActions] = useState<ActionResult[]>([]);
  const [selectedCache, setSelectedCache] = useState<Set<string>>(new Set());
  const [selectedOld, setSelectedOld] = useState<Set<string>>(new Set());
  const [reportTab, setReportTab] = useState<ReportTab>(INTENT_COPY[intent].tab);
  const [showScanForm, setShowScanForm] = useState(true);
  const [folderBrowser, setFolderBrowser] = useState<"scan" | "archive" | null>(null);
  const [archiveTargets, setArchiveTargets] = useState<ArchiveTarget[]>([]);
  const [archiveMode, setArchiveMode] = useState<"external" | "local">("external");
  const [externalPath, setExternalPath] = useState("");
  const [localArchivePath, setLocalArchivePath] = useState("");
  const [cleanPreview, setCleanPreview] = useState<ActionResult | null>(null);
  const [archivePreview, setArchivePreview] = useState<ActionResult | null>(null);
  const [cleanPreviewSignature, setCleanPreviewSignature] = useState("");
  const [archivePreviewSignature, setArchivePreviewSignature] = useState("");
  const [cleanPhrase, setCleanPhrase] = useState("");
  const [archivePhrase, setArchivePhrase] = useState("");
  const [lastResult, setLastResult] = useState<ActionResult | null>(null);
  const [mutationComplete, setMutationComplete] = useState(false);
  const [busyAction, setBusyAction] = useState(false);
  const [error, setError] = useState("");

  const cleanSignature = selectionSignature(selectedCache);
  const archiveDestination = archiveMode === "external" ? externalPath : localArchivePath.trim();
  const archiveSignature = selectionSignature(selectedOld, `${archiveMode}:${archiveDestination}`);
  const cleanPreviewCurrent = Boolean(cleanPreview && cleanPreviewSignature === cleanSignature);
  const archivePreviewCurrent = Boolean(archivePreview && archivePreviewSignature === archiveSignature);

  useEffect(() => {
    const next = INTENT_COPY[intent];
    setReportTab(next.tab);
    setShowScanForm(true);
    const needsDuplicates = intent === "duplicates" || intent === "full";
    setIncludeDuplicates(needsDuplicates);
    setIncludeNearDuplicates(needsDuplicates);
  }, [intent]);

  useEffect(() => {
    getConfig(token)
      .then((config) => setAgeMonths(config.default_age_months))
      .catch((err) => setError(err.message));
    refreshJobs().catch((err) => setError(err.message));
  }, [token]);

  useEffect(() => {
    if (!activeJob) return;
    if (activeJob.status === "completed") {
      loadReport(activeJob.id).catch((err) => setError(err.message));
      return;
    }
    if (activeJob.status === "failed") return;
    const events = openJobEvents(token, activeJob.id, (job) => {
      setActiveJob(job);
      setJobs((current) => mergeJob(current, job));
      if (job.status === "completed") {
        setError((current) => current.startsWith("Live progress was interrupted") ? "" : current);
        loadReport(job.id).catch((err) => setError(err.message));
      }
    });
    events.onerror = () => setError("Live progress was interrupted; the dashboard is reconnecting.");
    return () => events.close();
  }, [token, activeJob?.id, activeJob?.status]);

  useEffect(() => {
    if (reportTab !== "archive") return;
    listArchiveTargets(token)
      .then((response) => {
        setArchiveTargets(response.drives);
        if (!externalPath && response.recommended_path) setExternalPath(response.recommended_path);
        if (response.drives.length === 0 && !localArchivePath) setArchiveMode("local");
      })
      .catch((err) => setError(err.message));
  }, [token, reportTab]);

  async function refreshJobs() {
    const response = await listJobs(token);
    setJobs(response.jobs);
    onJobsChanged(response.jobs.length);
    if (!activeJob && response.jobs.length > 0) {
      setActiveJob(response.jobs[0]);
      setShowScanForm(false);
      if (response.jobs[0].status === "completed") await loadReport(response.jobs[0].id);
    }
  }

  async function loadReport(jobId: string) {
    const [nextReport, actionResponse] = await Promise.all([
      getReport(token, jobId),
      listJobActions(token, jobId)
    ]);
    setReport(nextReport);
    setActions(actionResponse.actions);
    setSelectedCache(new Set());
    setSelectedOld(new Set());
    setCleanPreview(null);
    setArchivePreview(null);
    setMutationComplete(false);
    setShowScanForm(false);
  }

  async function submitJob(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    setLastResult(null);
    const job = await createJob(token, {
      path: scanPath.trim() || undefined,
      age_months: ageMonths,
      include_duplicates: includeDuplicates,
      include_near_duplicates: includeDuplicates && includeNearDuplicates
    });
    setActiveJob(job);
    setReport(null);
    setActions([]);
    setJobs((current) => mergeJob(current, job));
    onJobsChanged(jobs.filter((item) => item.id !== job.id).length + 1);
    setShowScanForm(false);
  }

  async function selectJob(job: Job) {
    setActiveJob(job);
    setReport(null);
    setLastResult(null);
    setError("");
    setShowScanForm(false);
    if (job.status === "completed") await loadReport(job.id);
  }

  async function runPreview(action: "clean" | "archive") {
    if (!activeJob) return;
    setBusyAction(true);
    setError("");
    try {
      if (action === "clean") {
        const result = await runClean(token, activeJob.id, {
          candidate_ids: [...selectedCache],
          dry_run: true
        });
        setCleanPreview(result);
        setCleanPreviewSignature(cleanSignature);
        setCleanPhrase("");
        setLastResult(result);
      } else {
        if (!archiveDestination) throw new Error("Choose an archive destination first");
        const result = await runArchive(token, activeJob.id, {
          candidate_ids: [...selectedOld],
          dry_run: true,
          ...(archiveMode === "external"
            ? { external_path: archiveDestination }
            : { target_path: archiveDestination })
        });
        setArchivePreview(result);
        setArchivePreviewSignature(archiveSignature);
        setArchivePhrase("");
        setLastResult(result);
      }
      await refreshActions(activeJob.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Preview failed");
    } finally {
      setBusyAction(false);
    }
  }

  async function applyAction(action: "clean" | "archive") {
    if (!activeJob || !report) return;
    setBusyAction(true);
    setError("");
    try {
      const result = action === "clean"
        ? await runClean(token, activeJob.id, {
            candidate_ids: [...selectedCache],
            dry_run: false,
            confirmation_phrase: cleanPhrase
          })
        : await runArchive(token, activeJob.id, {
            candidate_ids: [...selectedOld],
            dry_run: false,
            confirmation_phrase: archivePhrase,
            ...(archiveMode === "external"
              ? { external_path: archiveDestination }
              : { target_path: archiveDestination })
          });
      setLastResult(result);
      setMutationComplete(true);
      if (action === "clean") {
        setCleanPreview(null);
        setCleanPhrase("");
      } else {
        setArchivePreview(null);
        setArchivePhrase("");
      }
      await refreshActions(activeJob.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : `${action} failed`);
    } finally {
      setBusyAction(false);
    }
  }

  async function refreshActions(jobId: string) {
    const response = await listJobActions(token, jobId);
    setActions(response.actions);
  }

  const currentJob = report?.job || activeJob;
  const intentCopy = INTENT_COPY[intent];

  return (
    <section className="reports-page">
      <header className="page-header">
        <div>
          <span className="eyebrow">Guided disk report</span>
          <h2>Reports and cleanup</h2>
          <p>Scan safely, understand what uses space, then preview any cleanup or archive action.</p>
        </div>
        <div className="button-row">
          <button onClick={() => onOpenHelp("reports")}><Info />How reports work</button>
          <button className="primary-inline" onClick={() => setShowScanForm((value) => !value)}>
            <FolderSearch />{showScanForm ? "Hide scan form" : "New scan"}
          </button>
        </div>
      </header>

      {error && <div className="alert" role="alert">{error}</div>}
      {mutationComplete && (
        <div className="stale-banner">
          <AlertTriangle />
          <div><strong>This report predates the completed file changes.</strong><span>Run a new scan to refresh sizes and candidates.</span></div>
          <button onClick={() => setShowScanForm(true)}><RefreshCw />Rescan</button>
        </div>
      )}

      {showScanForm && (
        <form className="scan-workflow panel" onSubmit={(event) => submitJob(event).catch((err) => setError(err.message))}>
          <div className="workflow-intro">
            <span className="step-number">1</span>
            <div><h3>{intentCopy.title}</h3><p>{intentCopy.description}</p></div>
          </div>
          <div className="form-grid">
            <div className="field-group field-span-2">
              <label htmlFor="scan-path">Folder to scan</label>
              <div className="input-with-button">
                <input
                  id="scan-path"
                  value={scanPath}
                  onChange={(event) => setScanPath(event.target.value)}
                  placeholder="Home folder (default)"
                />
                <button type="button" onClick={() => setFolderBrowser("scan")}><FolderOpen />Browse</button>
              </div>
              <small>Leave empty to scan your home folder. Reports never change files.</small>
            </div>
            <div className="field-group">
              <label htmlFor="age-months">Old-file age threshold</label>
              <div className="number-suffix"><input id="age-months" type="number" min="1" max="120" value={ageMonths} onChange={(event) => setAgeMonths(Number(event.target.value))} /><span>months</span></div>
            </div>
            <details className="advanced-options field-span-2" open={intent === "duplicates" || intent === "full"}>
              <summary>Duplicate analysis options</summary>
              <label className="checkbox-row">
                <input type="checkbox" checked={includeDuplicates} onChange={(event) => setIncludeDuplicates(event.target.checked)} />
                <span><strong>Exact duplicates</strong><small>Hashes same-size files to prove byte-identical content.</small></span>
              </label>
              <label className="checkbox-row">
                <input type="checkbox" checked={includeNearDuplicates} disabled={!includeDuplicates} onChange={(event) => setIncludeNearDuplicates(event.target.checked)} />
                <span><strong>Near duplicates</strong><small>Slower advisory comparison for supported text, image, audio, and video files.</small></span>
              </label>
            </details>
          </div>
          <div className="scan-review">
            <span className="step-number">2</span>
            <div>
              <h3>Review expected work</h3>
              <dl>
                <div><dt>Folder</dt><dd>{scanPath.trim() || "Home folder"}</dd></div>
                <div><dt>Old files</dt><dd>Last accessed at least {ageMonths} month{ageMonths === 1 ? "" : "s"} ago</dd></div>
                <div><dt>Duplicates</dt><dd>{includeDuplicates ? `Exact${includeNearDuplicates ? " and near" : " only"}` : "Skipped"}</dd></div>
              </dl>
            </div>
          </div>
          <div className="scan-submit-row">
            <div><span className="step-number">3</span><p>Start the background scan and follow its progress here.</p></div>
            <button type="submit" className="primary-inline"><FileSearch />Start scan</button>
          </div>
        </form>
      )}

      {jobs.length > 0 && (
        <section className="job-history" aria-label="Recent reports">
          <div className="section-heading"><div><History /><h3>Recent reports</h3></div><span>{jobs.length} saved</span></div>
          <div className="job-strip">
            {jobs.map((job) => (
              <button key={job.id} className={job.id === activeJob?.id ? "active" : ""} onClick={() => selectJob(job).catch((err) => setError(err.message))}>
                <span title={job.path}>{shortPath(job.path)}</span>
                <small>{formatDate(job.created_at)} · {job.status}</small>
              </button>
            ))}
          </div>
        </section>
      )}

      {currentJob && <JobStatus job={currentJob} />}
      {!currentJob && !showScanForm && (
        <section className="empty-state panel"><BarChart3 /><h2>No report selected</h2><p>Create a scan or choose a recent report.</p></section>
      )}
      {(currentJob?.status === "running" || currentJob?.status === "queued") && (
        <section className="loading-state panel"><Loader2 className="spin" /><strong>{currentJob.progress_phase}</strong><span>{currentJob.progress_message}</span></section>
      )}
      {currentJob?.status === "failed" && (
        <section className="alert"><strong>Report failed.</strong> {currentJob.error || currentJob.progress_message}</section>
      )}

      {report && (
        <>
          <ReportTabs active={reportTab} onChange={setReportTab} counts={{
            clean: report.candidate_counts.cache,
            archive: report.candidate_counts.old,
            activity: actions.length
          }} />
          {reportTab === "overview" && <OverviewTab report={report} />}
          {reportTab === "large" && <LargeItemsTab report={report} />}
          {reportTab === "duplicates" && <DuplicatesTab report={report} onHelp={() => onOpenHelp("duplicates")} />}
          {reportTab === "clean" && (
            <ActionWorkflow
              kind="clean"
              title="Clean cache files"
              description="Review cache-like files selected from this completed scan. A preview is required before deletion."
              candidates={report.cache_candidates}
              selected={selectedCache}
              setSelected={setSelectedCache}
              metadataKey="reason"
              preview={cleanPreview}
              previewCurrent={cleanPreviewCurrent}
              expectedPhrase={report.job.confirmation_phrases.clean}
              phrase={cleanPhrase}
              setPhrase={setCleanPhrase}
              busy={busyAction}
              lastResult={lastResult?.action_type === "clean" ? lastResult : null}
              onPreview={() => runPreview("clean")}
              onApply={() => applyAction("clean")}
              onHelp={() => onOpenHelp("clean")}
            />
          )}
          {reportTab === "archive" && (
            <ActionWorkflow
              kind="archive"
              title="Archive old files"
              description="Move selected files while preserving their relative paths and leaving symlinks at the originals."
              candidates={report.old_candidates}
              selected={selectedOld}
              setSelected={setSelectedOld}
              metadataKey="days_old"
              preview={archivePreview}
              previewCurrent={archivePreviewCurrent}
              expectedPhrase={report.job.confirmation_phrases.archive}
              phrase={archivePhrase}
              setPhrase={setArchivePhrase}
              busy={busyAction}
              lastResult={lastResult?.action_type === "archive" ? lastResult : null}
              destination={
                <ArchiveDestination
                  mode={archiveMode}
                  setMode={(next) => { setArchiveMode(next); setArchivePreview(null); }}
                  drives={archiveTargets}
                  externalPath={externalPath}
                  setExternalPath={(next) => { setExternalPath(next); setArchivePreview(null); }}
                  localPath={localArchivePath}
                  setLocalPath={(next) => { setLocalArchivePath(next); setArchivePreview(null); }}
                  onBrowse={() => setFolderBrowser("archive")}
                />
              }
              previewDisabled={!archiveDestination}
              onPreview={() => runPreview("archive")}
              onApply={() => applyAction("archive")}
              onHelp={() => onOpenHelp("archive")}
            />
          )}
          {reportTab === "activity" && <ActivityTab actions={actions} />}
        </>
      )}

      {folderBrowser && (
        <FolderBrowser
          token={token}
          initialPath={folderBrowser === "scan" ? scanPath : localArchivePath}
          title={folderBrowser === "scan" ? "Choose a folder to scan" : "Choose a local archive folder"}
          onClose={() => setFolderBrowser(null)}
          onSelect={(path) => {
            if (folderBrowser === "scan") setScanPath(path);
            else {
              setLocalArchivePath(path);
              setArchiveMode("local");
              setArchivePreview(null);
            }
            setFolderBrowser(null);
          }}
        />
      )}
    </section>
  );
}

function JobStatus({ job }: { job: Job }) {
  return (
    <section className="status-band">
      <div>
        <span className={`status-pill ${job.status}`}>{job.status}</span>
        <h2 title={job.path}>{job.path}</h2>
        <p>{job.progress_message}</p>
      </div>
      <div>
        <span className="progress-label">{Math.round(job.progress_percent)}%</span>
        <div className="progress-wrap" role="progressbar" aria-valuenow={job.progress_percent} aria-valuemin={0} aria-valuemax={100}>
          <div style={{ width: `${job.progress_percent}%` }} />
        </div>
      </div>
    </section>
  );
}

function ReportTabs({ active, onChange, counts }: {
  active: ReportTab;
  onChange: (tab: ReportTab) => void;
  counts: { clean: number; archive: number; activity: number };
}) {
  const tabs: Array<[ReportTab, string, number | null]> = [
    ["overview", "Overview", null],
    ["large", "Large items", null],
    ["duplicates", "Duplicates", null],
    ["clean", "Clean", counts.clean],
    ["archive", "Archive", counts.archive],
    ["activity", "Activity", counts.activity]
  ];
  return (
    <div className="report-tabs" role="tablist" aria-label="Report sections">
      {tabs.map(([id, label, count]) => (
        <button
          key={id}
          id={`report-tab-${id}`}
          role="tab"
          aria-controls={`report-panel-${id}`}
          aria-selected={active === id}
          tabIndex={active === id ? 0 : -1}
          className={active === id ? "active" : ""}
          onClick={() => onChange(id)}
          onKeyDown={(event) => {
            const index = tabs.findIndex(([tab]) => tab === id);
            let nextIndex: number | null = null;
            if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
            if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
            if (event.key === "Home") nextIndex = 0;
            if (event.key === "End") nextIndex = tabs.length - 1;
            if (nextIndex === null) return;
            event.preventDefault();
            const next = tabs[nextIndex][0];
            onChange(next);
            window.requestAnimationFrame(() => document.getElementById(`report-tab-${next}`)?.focus());
          }}
        >
          {label}{count !== null ? <span>{count}</span> : null}
        </button>
      ))}
    </div>
  );
}

function OverviewTab({ report }: { report: Report }) {
  const savingsData = [
    { name: "Cache", bytes: report.savings.cache_size },
    { name: "Old files", bytes: report.savings.old_files_size },
    { name: "Duplicates", bytes: report.savings.exact_duplicate_reclaimable_size }
  ];
  return (
    <section id="report-panel-overview" role="tabpanel" aria-labelledby="report-tab-overview" tabIndex={0} className="tab-content">
      <section className="metric-grid">
        <Metric label="Files scanned" value={String(report.scan.total_scanned)} />
        <Metric label="Total size" value={report.summary.total_size_formatted} />
        <Metric label="Potential savings" value={report.savings.total_savings_formatted} />
        <Metric label="Scan errors" value={String(report.scan.error_count)} />
      </section>
      <section className="chart-grid">
        <div className="panel chart-panel">
          <h2>File types</h2>
          {report.summary.top_extensions.length > 0 ? (
            <>
              <ResponsiveContainer width="100%" height={240}>
                <PieChart aria-label="File type size chart">
                  <Pie data={report.summary.top_extensions} dataKey="size" nameKey="extension" innerRadius={50} outerRadius={88} paddingAngle={2} stroke="var(--surface)">
                    {report.summary.top_extensions.map((entry, index) => <Cell key={entry.extension} fill={CHART_COLORS[index % CHART_COLORS.length]} />)}
                  </Pie>
                  <Tooltip {...CHART_TOOLTIP_PROPS} formatter={(value: number) => formatBytes(value)} />
                </PieChart>
              </ResponsiveContainer>
              <ul className="chart-legend">
                {report.summary.top_extensions.map((item, index) => (
                  <li key={item.extension}><i style={{ background: CHART_COLORS[index % CHART_COLORS.length] }} /><span>{item.extension || "No extension"}</span><strong>{item.size_formatted}</strong></li>
                ))}
              </ul>
            </>
          ) : <p className="muted">No file types were available.</p>}
        </div>
        <div className="panel chart-panel">
          <h2>Potential savings</h2>
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={savingsData} aria-label="Potential savings chart">
              <CartesianGrid vertical={false} stroke="var(--line)" />
              <XAxis dataKey="name" {...CHART_AXIS_PROPS} />
              <YAxis tickFormatter={formatBytes} width={72} {...CHART_AXIS_PROPS} />
              <Tooltip {...CHART_TOOLTIP_PROPS} cursor={{ fill: "rgb(96 165 250 / 8%)" }} formatter={(value: number) => formatBytes(value)} />
              <Bar dataKey="bytes" fill="var(--chart-1)" radius={[5, 5, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
          <ul className="chart-legend compact">
            {savingsData.map((item) => <li key={item.name}><span>{item.name}</span><strong>{formatBytes(item.bytes)}</strong></li>)}
          </ul>
        </div>
      </section>
      <section className="panel">
        <div className="section-heading"><div><AlertTriangle /><h2>Scan issues</h2></div><span>{report.scan.error_count}</span></div>
        {report.scan.errors.length === 0 ? <p className="success-copy"><CheckCircle2 />No permission or read errors were recorded.</p> : (
          <ul className="error-list">{report.scan.errors.map((error, index) => <li key={`${error}-${index}`}>{error}</li>)}</ul>
        )}
      </section>
    </section>
  );
}

function LargeItemsTab({ report }: { report: Report }) {
  return (
    <section id="report-panel-large" role="tabpanel" aria-labelledby="report-tab-large" tabIndex={0} className="tab-content">
      <div className="section-intro panel"><BarChart3 /><div><h2>Largest items</h2><p>Use this read-only view to understand where storage is concentrated.</p></div></div>
      <section className="tables-grid">
        <SizeTable title="Largest directories" rows={report.largest_directories} />
        <SizeTable title="Largest files" rows={report.largest_files} />
      </section>
    </section>
  );
}

function DuplicatesTab({ report, onHelp }: { report: Report; onHelp: () => void }) {
  const exact = report.duplicates.exact.groups || [];
  const near = report.duplicates.near.groups || [];
  return (
    <section id="report-panel-duplicates" role="tabpanel" aria-labelledby="report-tab-duplicates" tabIndex={0} className="tab-content">
      <div className="advisory-banner"><Info /><div><strong>Duplicate results are review-only.</strong><span>The app never automatically removes a file from these groups.</span></div><button onClick={onHelp}>Learn how matching works</button></div>
      {exact.length === 0 && near.length === 0 ? (
        <section className="empty-state panel"><CopyCheck /><h2>No duplicate groups found</h2><p>Run another report with duplicate options enabled if they were skipped.</p></section>
      ) : (
        <section className="duplicate-grid">
          <DuplicateGroupList title="Exact duplicates" groups={exact} metric={report.duplicates.exact.reclaimable_size_formatted || "0 B"} />
          <DuplicateGroupList title="Near duplicates" groups={near} metric={report.duplicates.near.reviewable_size_formatted || "0 B"} />
        </section>
      )}
    </section>
  );
}

function ActionWorkflow({
  kind,
  title,
  description,
  candidates,
  selected,
  setSelected,
  metadataKey,
  preview,
  previewCurrent,
  expectedPhrase,
  phrase,
  setPhrase,
  busy,
  lastResult,
  destination,
  previewDisabled = false,
  onPreview,
  onApply,
  onHelp
}: {
  kind: "clean" | "archive";
  title: string;
  description: string;
  candidates: Candidate[];
  selected: Set<string>;
  setSelected: (next: Set<string>) => void;
  metadataKey: string;
  preview: ActionResult | null;
  previewCurrent: boolean;
  expectedPhrase: string;
  phrase: string;
  setPhrase: (value: string) => void;
  busy: boolean;
  lastResult: ActionResult | null;
  destination?: ReactNode;
  previewDisabled?: boolean;
  onPreview: () => Promise<void>;
  onApply: () => Promise<void>;
  onHelp: () => void;
}) {
  const selectedBytes = candidates.filter((candidate) => selected.has(candidate.id)).reduce((total, item) => total + item.size, 0);
  return (
    <section id={`report-panel-${kind}`} role="tabpanel" aria-labelledby={`report-tab-${kind}`} tabIndex={0} className="tab-content action-workflow">
      <header className="action-header panel">
        <div className={`action-icon ${kind}`}>{kind === "clean" ? <Trash2 /> : <Archive />}</div>
        <div><span className="eyebrow">Preview required</span><h2>{title}</h2><p>{description}</p></div>
        <button onClick={onHelp}><Info />Safety guide</button>
      </header>
      {destination}
      <section className="panel">
        <div className="section-heading"><div><Search /><h2>Choose files</h2></div><span>{selected.size} selected · {formatBytes(selectedBytes)}</span></div>
        <CandidateTable candidates={candidates} selected={selected} setSelected={setSelected} metadataKey={metadataKey} />
      </section>
      <section className="preview-grid">
        <div className="panel preview-step">
          <span className="step-number">1</span>
          <div><h2>Preview safely</h2><p>Simulate this exact selection{kind === "archive" ? " and destination" : ""}. No files change.</p></div>
          <button
            className="primary-inline"
            disabled={busy || selected.size === 0 || previewDisabled}
            title={selected.size === 0 ? "Select at least one candidate" : previewDisabled ? "Choose an archive destination" : ""}
            onClick={() => onPreview().catch(() => undefined)}
          >
            {busy ? <Loader2 className="spin" /> : <FileSearch />}Run dry-run preview
          </button>
        </div>
        <div className={!previewCurrent ? "panel confirm-step disabled-step" : "panel confirm-step"}>
          <span className="step-number">2</span>
          <div><h2>Confirm real changes</h2><p>{previewCurrent ? "The current selection matches the preview." : "Run or refresh the preview to unlock confirmation."}</p></div>
          {previewCurrent && preview && (
            <>
              <ActionSummary result={preview} />
              <label htmlFor={`${kind}-confirmation`}>Type this exact phrase</label>
              <code className="confirmation-code">{expectedPhrase}</code>
              <input id={`${kind}-confirmation`} value={phrase} onChange={(event) => setPhrase(event.target.value)} autoComplete="off" />
              <button className="danger-button" disabled={busy || phrase !== expectedPhrase} onClick={() => onApply().catch(() => undefined)}>
                {kind === "clean" ? "Delete selected files" : "Archive selected files"}
              </button>
            </>
          )}
        </div>
      </section>
      {lastResult && <section className={lastResult.dry_run ? "result-panel panel" : "result-panel panel completed-result"}><CheckCircle2 /><div><h2>{lastResult.dry_run ? "Preview complete" : "Action complete"}</h2><ActionSummary result={lastResult} /></div></section>}
    </section>
  );
}

function ArchiveDestination({ mode, setMode, drives, externalPath, setExternalPath, localPath, setLocalPath, onBrowse }: {
  mode: "external" | "local";
  setMode: (mode: "external" | "local") => void;
  drives: ArchiveTarget[];
  externalPath: string;
  setExternalPath: (path: string) => void;
  localPath: string;
  setLocalPath: (path: string) => void;
  onBrowse: () => void;
}) {
  return (
    <section className="panel destination-panel">
      <div className="section-heading"><div><HardDrive /><h2>Choose destination</h2></div><span>Required before preview</span></div>
      <div className="destination-options">
        <label className={mode === "external" ? "choice-card active" : "choice-card"}>
          <input type="radio" name="archive-mode" checked={mode === "external"} disabled={drives.length === 0} onChange={() => setMode("external")} />
          <HardDrive /><span><strong>Connected external drive</strong><small>{drives.length ? "Recommended for freeing local space" : "No writable external drives detected"}</small></span>
        </label>
        <label className={mode === "local" ? "choice-card active" : "choice-card"}>
          <input type="radio" name="archive-mode" checked={mode === "local"} onChange={() => setMode("local")} />
          <FolderOpen /><span><strong>Local folder</strong><small>Useful for reorganizing or testing the archive workflow</small></span>
        </label>
      </div>
      {mode === "external" ? (
        <div className="field-group">
          <label htmlFor="external-target">External drive</label>
          <select id="external-target" value={externalPath} onChange={(event) => setExternalPath(event.target.value)}>
            <option value="">Choose a drive</option>
            {drives.map((drive, index) => <option key={drive.path} value={drive.path}>{index === 0 ? "Recommended · " : ""}{drive.name} · {formatBytes(drive.available_space)} free</option>)}
          </select>
          {externalPath && <small className="path-hint">Files will be stored under {externalPath}/archived_files.</small>}
        </div>
      ) : (
        <div className="field-group">
          <label htmlFor="local-target">Local archive folder</label>
          <div className="input-with-button"><input id="local-target" value={localPath} onChange={(event) => setLocalPath(event.target.value)} placeholder="/path/to/archive" /><button onClick={onBrowse}><FolderOpen />Browse</button></div>
          <small>The folder may be new; it will be created only during the real archive action.</small>
        </div>
      )}
    </section>
  );
}

function CandidateTable({ candidates, selected, setSelected, metadataKey }: {
  candidates: Candidate[];
  selected: Set<string>;
  setSelected: (next: Set<string>) => void;
  metadataKey: string;
}) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const filtered = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return normalized ? candidates.filter((candidate) => candidate.path.toLowerCase().includes(normalized)) : candidates;
  }, [candidates, query]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, pageCount - 1);
  const visible = filtered.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);
  const pageSelected = visible.length > 0 && visible.every((candidate) => selected.has(candidate.id));
  const allMatchingSelected = filtered.length > 0 && filtered.every((candidate) => selected.has(candidate.id));

  function setIds(items: Candidate[], checked: boolean) {
    const next = new Set(selected);
    items.forEach((item) => checked ? next.add(item.id) : next.delete(item.id));
    setSelected(next);
  }

  return (
    <div className="candidate-browser">
      <div className="candidate-toolbar">
        <label className="search-field compact" htmlFor={`candidate-search-${metadataKey}`}><Search /><span className="sr-only">Search candidates</span><input id={`candidate-search-${metadataKey}`} value={query} onChange={(event) => { setQuery(event.target.value); setPage(0); }} placeholder="Filter by path" /></label>
        <div className="button-row">
          <button onClick={() => setIds(visible, !pageSelected)}>{pageSelected ? "Clear page" : "Select page"}</button>
          <button onClick={() => setIds(filtered, !allMatchingSelected)}>{allMatchingSelected ? "Clear matching" : `Select all ${filtered.length} matching`}</button>
          {selected.size > 0 && <button onClick={() => setSelected(new Set())}>Clear selection</button>}
        </div>
      </div>
      <div className="table-scroll">
        <table>
          <thead><tr><th scope="col">Select</th><th scope="col">Path</th><th scope="col">Size</th><th scope="col">{metadataKey === "days_old" ? "Age" : "Reason"}</th></tr></thead>
          <tbody>
            {visible.map((candidate) => (
              <tr key={candidate.id}>
                <td><input type="checkbox" checked={selected.has(candidate.id)} onChange={() => setIds([candidate], !selected.has(candidate.id))} aria-label={`Select ${candidate.path}`} /></td>
                <td><PathDisclosure path={candidate.path} /></td>
                <td className="nowrap">{formatBytes(candidate.size)}</td>
                <td>{formatMetadata(candidate.metadata[metadataKey], metadataKey)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {visible.length === 0 && <p className="empty-table">No candidates match this filter.</p>}
      </div>
      <div className="pagination"><span>Showing {filtered.length ? currentPage * PAGE_SIZE + 1 : 0}–{Math.min((currentPage + 1) * PAGE_SIZE, filtered.length)} of {filtered.length}</span><div><button disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous</button><span>Page {currentPage + 1} of {pageCount}</span><button disabled={currentPage >= pageCount - 1} onClick={() => setPage(currentPage + 1)}>Next</button></div></div>
    </div>
  );
}

function ActivityTab({ actions }: { actions: ActionResult[] }) {
  return (
    <section id="report-panel-activity" role="tabpanel" aria-labelledby="report-tab-activity" tabIndex={0} className="tab-content">
      <div className="section-intro panel"><History /><div><h2>Action history</h2><p>Dry-run previews and completed actions are stored with this report.</p></div></div>
      {actions.length === 0 ? <section className="empty-state panel"><History /><h2>No actions yet</h2><p>Preview a clean or archive selection to create the first entry.</p></section> : (
        <div className="activity-list">{actions.map((action) => <article className="panel" key={action.id}><div><span className={`status-pill ${action.dry_run ? "queued" : "completed"}`}>{action.dry_run ? "preview" : "completed"}</span><h3>{action.action_type}</h3><small>{formatDate(action.created_at)}</small></div><ActionSummary result={action} /></article>)}</div>
      )}
    </section>
  );
}

function ActionSummary({ result }: { result: ActionResult }) {
  const entries: Array<[string, string]> = [
    ["Selected", String(result.selected_count)],
    ["Skipped", String(result.skipped_count)]
  ];
  if (result.target_path) entries.push(["Destination", result.target_path]);
  Object.entries(result.result).forEach(([key, value]) => {
    if (["logs", "actions"].includes(key) || typeof value === "object") return;
    const formatted = typeof value === "number" && /(size|bytes)/i.test(key) ? formatBytes(value) : String(value);
    entries.push([humanize(key), formatted]);
  });
  return <dl className="result-summary">{entries.map(([label, value]) => <div key={`${label}-${value}`}><dt>{label}</dt><dd title={value}>{value}</dd></div>)}</dl>;
}

function SizeTable({ title, rows }: { title: string; rows: { path: string; size_formatted: string }[] }) {
  return <div className="panel"><h2>{title}</h2><div className="table-scroll"><table><thead><tr><th scope="col">Path</th><th scope="col">Size</th></tr></thead><tbody>{rows.map((row) => <tr key={row.path}><td><PathDisclosure path={row.path} /></td><td className="nowrap">{row.size_formatted}</td></tr>)}</tbody></table></div></div>;
}

function DuplicateGroupList({ title, groups, metric }: { title: string; groups: DuplicateGroup[]; metric: string }) {
  return <div className="panel"><div className="section-heading"><div><CopyCheck /><h2>{title}</h2></div><span>{metric}</span></div><div className="duplicate-list">{groups.map((group, index) => <article key={`${title}-${index}`} className="duplicate-group"><header><strong>{group.file_count} files</strong><span>{group.reclaimable_size_formatted || group.reviewable_size_formatted || group.total_size_formatted || ""}</span></header>{group.reason && <p>{group.reason}</p>}<ul>{group.files.map((file) => <li key={file.path}><PathDisclosure path={file.path} /><small>{file.size_formatted}</small></li>)}</ul></article>)}</div></div>;
}

function PathDisclosure({ path }: { path: string }) {
  const name = path.split("/").filter(Boolean).pop() || path;
  return <details className="path-disclosure"><summary title={path}>{name}<ChevronRight /></summary><code>{path}</code></details>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="metric"><span>{label}</span><strong>{value}</strong></div>;
}

function selectionSignature(selected: Set<string>, suffix = "") {
  return `${[...selected].sort().join("|")}::${suffix}`;
}

function mergeJob(jobs: Job[], job: Job): Job[] {
  return [job, ...jobs.filter((item) => item.id !== job.id)];
}

function shortPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts.slice(-2).join("/") || path;
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value)) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${amount.toFixed(amount >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatMetadata(value: unknown, key: string): string {
  if (key === "days_old" && typeof value === "number") return `${Math.round(value)} days`;
  return String(value ?? "");
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function humanize(value: string): string {
  return value.replace(/_/g, " ").replace(/^./, (letter) => letter.toUpperCase());
}
