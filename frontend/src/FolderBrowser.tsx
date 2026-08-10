import { useEffect, useRef, useState } from "react";
import { ChevronLeft, Folder, FolderOpen, HardDrive, Home, Loader2, X } from "lucide-react";

import { browseDirectories } from "./api";
import type { DirectoryListing, DirectoryLocation } from "./types";

export function FolderBrowser({
  token,
  initialPath,
  title = "Choose a folder",
  onSelect,
  onClose
}: {
  token: string;
  initialPath?: string;
  title?: string;
  onSelect: (path: string) => void;
  onClose: () => void;
}) {
  const [listing, setListing] = useState<DirectoryListing | null>(null);
  const [pathInput, setPathInput] = useState(initialPath || "");
  const [showHidden, setShowHidden] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);

  async function load(path?: string, hidden = showHidden) {
    setLoading(true);
    setError("");
    try {
      const next = await browseDirectories(token, path, hidden);
      setListing(next);
      setPathInput(next.current_path);
    } catch (err) {
      setError(err instanceof Error ? err.message : "The folder could not be opened");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(initialPath || undefined).catch(() => undefined);
    closeRef.current?.focus();
  }, []);

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          "button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])"
        )
      );
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
  }, [onClose]);

  return (
    <div className="modal-backdrop" role="presentation">
      <section ref={dialogRef} className="folder-browser" role="dialog" aria-modal="true" aria-labelledby="folder-browser-title">
        <header className="modal-header">
          <div>
            <span className="eyebrow">Server filesystem</span>
            <h2 id="folder-browser-title">{title}</h2>
          </div>
          <button ref={closeRef} className="icon-button" onClick={onClose} aria-label="Close folder browser">
            <X />
          </button>
        </header>

        <div className="folder-roots" aria-label="Folder shortcuts">
          {listing?.roots.map((root) => (
            <button key={root.path} disabled={!root.readable} title={root.readable ? root.path : "This location is not readable by the server process"} onClick={() => load(root.path)}>
              <RootIcon root={root} />
              <span>{root.name}</span>
            </button>
          ))}
        </div>

        <form
          className="path-navigator"
          onSubmit={(event) => {
            event.preventDefault();
            load(pathInput).catch(() => undefined);
          }}
        >
          <button
            type="button"
            className="icon-button"
            disabled={!listing?.parent_path}
            onClick={() => load(listing?.parent_path || undefined)}
            aria-label="Open parent folder"
          >
            <ChevronLeft />
          </button>
          <label className="sr-only" htmlFor="folder-browser-path">Folder path</label>
          <input
            id="folder-browser-path"
            value={pathInput}
            onChange={(event) => setPathInput(event.target.value)}
          />
          <button type="submit">Go</button>
        </form>

        <label className="checkbox-row compact-check">
          <input
            type="checkbox"
            checked={showHidden}
            onChange={(event) => {
              const next = event.target.checked;
              setShowHidden(next);
              load(listing?.current_path || initialPath || undefined, next).catch(() => undefined);
            }}
          />
          Show hidden folders
        </label>

        {error && <div className="alert" role="alert">{error}</div>}
        <div className="folder-list" aria-live="polite">
          {loading && <div className="loading-inline"><Loader2 className="spin" />Opening folder…</div>}
          {!loading && listing?.directories.length === 0 && (
            <p className="muted">This folder has no visible subfolders.</p>
          )}
          {!loading && listing?.directories.map((directory) => (
            <button
              key={directory.path}
              disabled={!directory.readable}
              onClick={() => load(directory.path)}
              title={directory.readable ? directory.path : "This folder is not readable by the server process"}
            >
              <Folder />
              <span>{directory.name}</span>
              {directory.symlink && <small>shortcut</small>}
            </button>
          ))}
        </div>

        <footer className="modal-actions">
          <button onClick={onClose}>Cancel</button>
          <button
            className="primary-inline"
            disabled={!listing || loading}
            onClick={() => listing && onSelect(listing.current_path)}
          >
            <FolderOpen />Use this folder
          </button>
        </footer>
      </section>
    </div>
  );
}

function RootIcon({ root }: { root: DirectoryLocation }) {
  if (root.kind === "home") return <Home />;
  if (root.kind === "drive") return <HardDrive />;
  return <FolderOpen />;
}
