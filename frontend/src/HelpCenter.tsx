import { BookOpen, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

type HelpArticle = {
  id: string;
  category: string;
  title: string;
  summary: string;
  steps: string[];
  notes?: string[];
};

const ARTICLES: HelpArticle[] = [
  {
    id: "start",
    category: "Getting started",
    title: "Start and connect to the dashboard",
    summary: "Run one local command, then work entirely in the authenticated browser dashboard.",
    steps: [
      "From the project folder, run: uv run disk-space-manager-web",
      "The server prints an authenticated local URL and normally opens it automatically.",
      "If a browser cannot be opened, copy the printed URL. Use --no-open when you prefer that behavior.",
      "Keep the token private. It authorizes filesystem reports and confirmed actions on this computer."
    ],
    notes: ["The server binds to 127.0.0.1 by default. Network mode should be enabled only intentionally."]
  },
  {
    id: "browse",
    category: "Browse",
    title: "Browse folders by size",
    summary: "Measure one filesystem level at a time and start a safe protected offload for inactive folders.",
    steps: [
      "Open Browse. The home folder is selected by default, and every immediate file and folder is listed.",
      "Wait for recursive folder sizing to finish. On-disk size is sorted largest first; logical size shows file content length.",
      "Open a folder name to navigate deeper. Hidden items stay included in totals even when their rows are hidden.",
      "Choose Protect & offload on an eligible folder, then complete recovery-key, replica, verification, preview, and eviction steps in Protected folders."
    ],
    notes: [
      "Browse never reads file contents or changes files.",
      "Symbolic links and nested mounted filesystems are not followed.",
      "Allocated size is advisory: APFS clones, snapshots, hard links, compression, permissions, and skipped mounts can make it differ from macOS storage usage or reclaimable space."
    ]
  },
  {
    id: "reports",
    category: "Reports",
    title: "Analyze disk usage",
    summary: "Scan a folder to see its size, largest items, cleanup candidates, and optional duplicates.",
    steps: [
      "Open Reports and choose a folder. The home folder is the default when no path is entered.",
      "Set the age threshold used to identify old files.",
      "Leave exact duplicates enabled for byte-identical matches. Near-duplicate media checks take longer.",
      "Start the scan and keep the page open to watch queued, scanning, analysis, and completion progress."
    ],
    notes: ["Reports are read-only. Permission errors are listed in the Overview tab."]
  },
  {
    id: "clean",
    category: "Free space",
    title: "Review and clean cache files",
    summary: "Delete selected cache-like files through a required preview and confirmation flow.",
    steps: [
      "Create or open a completed report and select the Clean tab.",
      "Search and select candidates. Review the reason and size for every selection.",
      "Run the dry-run preview. It logs the proposed action but does not remove files.",
      "If the preview is correct, type the displayed CLEAN phrase exactly and apply the action."
    ],
    notes: ["Changing the selection invalidates the preview.", "Registered protected-folder paths are skipped."]
  },
  {
    id: "archive",
    category: "Free space",
    title: "Archive old files",
    summary: "Move selected old files to an external drive or local folder while preserving their structure.",
    steps: [
      "Open the Archive tab of a completed report and select old-file candidates.",
      "Choose a detected external drive or a local destination folder.",
      "Preview the exact selection and destination, then type the displayed ARCHIVE phrase.",
      "After copying and verifying each file, the original is replaced with a symlink to the archived copy."
    ],
    notes: ["Restore access requires the archive destination to remain mounted.", "The destination is excluded from unsafe self-archiving."]
  },
  {
    id: "duplicates",
    category: "Reports",
    title: "Understand duplicate results",
    summary: "Exact matches estimate reclaimable space; near matches are advisory review groups.",
    steps: [
      "Exact duplicate groups contain files with matching streamed SHA-256 content hashes.",
      "Near-duplicate text, images, audio, and video use offline similarity fingerprints.",
      "Inspect every path and keep the version you trust. Disk Space Manager does not delete duplicates automatically."
    ],
    notes: ["Near-duplicate size is reviewable, not guaranteed reclaimable space."]
  },
  {
    id: "protect-setup",
    category: "Protected folders",
    title: "Create a protected folder",
    summary: "Create independent encrypted Restic repositories that can be connected one disk at a time.",
    steps: [
      "Protected folders currently require macOS and Restic 0.19.1 or newer.",
      "Create a folder entry and save the one-time recovery key outside this application.",
      "Paste the key back to confirm recovery before enrolling a writable external disk.",
      "Index the local folder, back it up to one disk, then repeat with each required independent disk."
    ],
    notes: ["Store replica disks separately when offline.", "The app never runs Restic forget or prune automatically."]
  },
  {
    id: "protect-checks",
    category: "Protected folders",
    title: "Back up and verify replicas",
    summary: "Structural checks prove repository consistency; full checks read stored data blocks back.",
    steps: [
      "Index after local files change so the catalog represents the current generation.",
      "Back up the generation to a mounted enrolled disk. A structural check follows automatically.",
      "Use Check for repository structure or Full check for bit-for-bit data-block readback.",
      "Select catalog files and a mounted drive when you only need a targeted full verification."
    ]
  },
  {
    id: "evict-restore",
    category: "Protected folders",
    title: "Evict and restore protected files",
    summary: "Remove local files only after the configured verified replica count has been proven.",
    steps: [
      "Select local files and preview eviction. Every selected file must be eligible.",
      "Review verified copy counts and the structural/full verification warning.",
      "Type the exact EVICT phrase. No symlink or placeholder remains after eviction.",
      "To restore, connect a healthy enrolled disk, select catalog files, and choose the original or another destination."
    ],
    notes: ["Restore never overwrites an existing path.", "Eviction and restore are recorded in the action log and protected operation history."]
  },
  {
    id: "recovery",
    category: "Recovery",
    title: "Recover on a fresh Mac",
    summary: "Rebuild the offline catalog from either independent repository and the saved recovery key.",
    steps: [
      "Install Disk Space Manager and Restic, then connect either enrolled disk.",
      "Choose Recover, select the disk, enter the future restore path, required copy count, and saved recovery key.",
      "If the disk contains multiple protected sets, provide the set ID shown in its repository marker.",
      "After import, restore catalog files normally from any connected healthy replica."
    ]
  },
  {
    id: "troubleshooting",
    category: "Reference",
    title: "Troubleshooting and glossary",
    summary: "Resolve common path, drive, token, and protection-status questions.",
    steps: [
      "Queued means another disk-heavy web operation is using the single safety queue.",
      "Structural means Restic checked repository structure; full means stored data blocks were read.",
      "Pending means a current file version has fewer verified replicas than required.",
      "At risk means an evicted file no longer has enough healthy verified replica receipts.",
      "If a path is rejected, verify that it exists, is readable, and is visible to the server process.",
      "If authentication fails, reopen the URL printed by the running server or reconnect with its token."
    ]
  }
];

export function HelpCenter({ initialArticle = "start", onOpenReports, onOpenProtection, onOpenBrowse = () => undefined }: {
  initialArticle?: string;
  onOpenReports: () => void;
  onOpenProtection: () => void;
  onOpenBrowse?: () => void;
}) {
  const [query, setQuery] = useState("");
  const [activeId, setActiveId] = useState(initialArticle);
  useEffect(() => setActiveId(initialArticle), [initialArticle]);
  const matches = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return ARTICLES;
    return ARTICLES.filter((article) =>
      [article.title, article.category, article.summary, ...article.steps, ...(article.notes || [])]
        .join(" ")
        .toLowerCase()
        .includes(normalized)
    );
  }, [query]);
  const active = matches.find((article) => article.id === activeId) || matches[0];

  return (
    <section className="help-center">
      <header className="page-header">
        <div>
          <span className="eyebrow">Documentation built into the dashboard</span>
          <h2>Help center</h2>
          <p>Learn what each operation does, when it changes files, and how to recover safely.</p>
        </div>
      </header>
      <label className="search-field" htmlFor="help-search">
        <Search />
        <span className="sr-only">Search help</span>
        <input
          id="help-search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search help, for example “archive” or “full check”"
        />
      </label>
      <div className="help-layout">
        <nav className="help-nav panel" aria-label="Help articles">
          {matches.map((article) => (
            <button
              key={article.id}
              className={article.id === active?.id ? "active" : ""}
              onClick={() => setActiveId(article.id)}
            >
              <small>{article.category}</small>
              <span>{article.title}</span>
            </button>
          ))}
          {matches.length === 0 && <p className="muted">No help articles match “{query}”.</p>}
        </nav>
        {active && (
          <article className="help-article panel">
            <BookOpen />
            <span className="eyebrow">{active.category}</span>
            <h2>{active.title}</h2>
            <p className="lead">{active.summary}</p>
            <ol>
              {active.steps.map((step) => <li key={step}>{renderCommand(step)}</li>)}
            </ol>
            {active.notes && (
              <div className="help-notes">
                {active.notes.map((note) => <p key={note}>{note}</p>)}
              </div>
            )}
            <div className="button-row">
              {active.category === "Reports" || active.category === "Free space" ? (
                <button className="primary-inline" onClick={onOpenReports}>Open reports</button>
              ) : null}
              {active.category === "Protected folders" || active.category === "Recovery" ? (
                <button className="primary-inline" onClick={onOpenProtection}>Open protected folders</button>
              ) : null}
              {active.category === "Browse" ? (
                <button className="primary-inline" onClick={onOpenBrowse}>Open Browse</button>
              ) : null}
            </div>
          </article>
        )}
      </div>
    </section>
  );
}

function renderCommand(text: string) {
  const command = "uv run disk-space-manager-web";
  if (!text.includes(command)) return text;
  const [before, after] = text.split(command);
  return <>{before}<code>{command}</code>{after}</>;
}
