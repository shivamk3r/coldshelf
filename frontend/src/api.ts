import type {
  ActionResult,
  AppConfig,
  ArchiveTarget,
  BrowseDeletion,
  BrowseDeletionPreview,
  BrowseResult,
  BrowseScan,
  BrowseScanCreated,
  DirectoryListing,
  DriveTarget,
  Job,
  MountedVolume,
  ProtectedFolder,
  ProtectedFolderCreated,
  ProtectedFolderDetail,
  ProtectionPreflight,
  ProtectionOperation,
  Report,
  StorageSummary
} from "./types";

const API_BASE = import.meta.env.VITE_COLDSHELF_API_BASE || import.meta.env.VITE_DSM_API_BASE || "";
const ENV_TOKEN = import.meta.env.VITE_COLDSHELF_TOKEN || import.meta.env.VITE_DSM_TOKEN || "";

export function initialToken(): string {
  const params = new URLSearchParams(window.location.search);
  const queryToken = params.get("token") || "";
  if (queryToken) {
    params.delete("token");
    const query = params.toString();
    window.history.replaceState(
      {},
      "",
      `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`
    );
  }
  return queryToken || ENV_TOKEN || localStorage.getItem("coldshelf-token") || localStorage.getItem("dsm-token") || "";
}

export function saveToken(token: string): void {
  localStorage.setItem("coldshelf-token", token);
  localStorage.removeItem("dsm-token");
}

export function clearToken(): void {
  localStorage.removeItem("coldshelf-token");
  localStorage.removeItem("dsm-token");
}

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function apiRequest<T>(
  path: string,
  token: string,
  init: RequestInit = {}
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(init.headers || {})
    }
  });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch {
      // Keep HTTP status text when the body is not JSON.
    }
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}

export function getConfig(token: string): Promise<AppConfig> {
  return apiRequest<AppConfig>("/api/config", token);
}

export function getStorageSummary(token: string): Promise<StorageSummary> {
  return apiRequest<StorageSummary>("/api/storage", token);
}

export function browseDirectories(
  token: string,
  path?: string,
  showHidden = false
): Promise<DirectoryListing> {
  const params = new URLSearchParams();
  if (path) params.set("path", path);
  if (showHidden) params.set("show_hidden", "true");
  const suffix = params.toString();
  return apiRequest<DirectoryListing>(
    `/api/filesystem/directories${suffix ? `?${suffix}` : ""}`,
    token
  );
}

export function createBrowseScan(
  token: string,
  path?: string
): Promise<BrowseScanCreated> {
  return apiRequest<BrowseScanCreated>("/api/browse/scans", token, {
    method: "POST",
    body: JSON.stringify({ ...(path ? { path } : {}) })
  });
}

export function getBrowseResult(token: string, scanId: string): Promise<BrowseResult> {
  return apiRequest<BrowseResult>(`/api/browse/scans/${scanId}/result`, token);
}

export function cancelBrowseScan(token: string, scanId: string): Promise<BrowseScan> {
  return apiRequest<BrowseScan>(`/api/browse/scans/${scanId}/cancel`, token, {
    method: "POST"
  });
}

export function openBrowseEvents(
  token: string,
  scanId: string,
  onScan: (scan: BrowseScan) => void
): EventSource {
  const source = new EventSource(
    `${API_BASE}/api/browse/scans/${scanId}/events?token=${encodeURIComponent(token)}`
  );
  source.addEventListener("progress", (event) => {
    const scan = JSON.parse((event as MessageEvent).data) as BrowseScan;
    onScan(scan);
    if (["completed", "failed", "cancelled"].includes(scan.status)) source.close();
  });
  return source;
}

export function previewBrowseDeletion(
  token: string,
  scanId: string,
  paths: string[]
): Promise<BrowseDeletionPreview> {
  return apiRequest<BrowseDeletionPreview>("/api/browse/deletion-previews", token, {
    method: "POST",
    body: JSON.stringify({ scan_id: scanId, paths })
  });
}

export function scheduleBrowseDeletion(
  token: string,
  previewId: string
): Promise<BrowseDeletion> {
  return apiRequest<BrowseDeletion>("/api/browse/deletions", token, {
    method: "POST",
    body: JSON.stringify({ preview_id: previewId })
  });
}

export function getActiveBrowseDeletion(
  token: string
): Promise<{ deletion: BrowseDeletion | null }> {
  return apiRequest<{ deletion: BrowseDeletion | null }>(
    "/api/browse/deletions/active",
    token
  );
}

export function getBrowseDeletion(
  token: string,
  deletionId: string
): Promise<BrowseDeletion> {
  return apiRequest<BrowseDeletion>(`/api/browse/deletions/${deletionId}`, token);
}

export function undoBrowseDeletion(
  token: string,
  deletionId: string
): Promise<BrowseDeletion> {
  return apiRequest<BrowseDeletion>(
    `/api/browse/deletions/${deletionId}/undo`,
    token,
    { method: "POST" }
  );
}

export function listArchiveTargets(
  token: string
): Promise<{ drives: ArchiveTarget[]; recommended_path?: string | null }> {
  return apiRequest<{ drives: ArchiveTarget[]; recommended_path?: string | null }>(
    "/api/archive-targets",
    token
  );
}

export function createJob(
  token: string,
  payload: {
    path?: string;
    age_months: number;
    include_duplicates: boolean;
    include_near_duplicates: boolean;
  }
): Promise<Job> {
  return apiRequest<Job>("/api/jobs", token, {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function listJobs(token: string): Promise<{ jobs: Job[] }> {
  return apiRequest<{ jobs: Job[] }>("/api/jobs", token);
}

export function getReport(token: string, jobId: string): Promise<Report> {
  return apiRequest<Report>(`/api/jobs/${jobId}/report`, token);
}

export function listJobActions(
  token: string,
  jobId: string
): Promise<{ actions: ActionResult[] }> {
  return apiRequest<{ actions: ActionResult[] }>(
    `/api/jobs/${jobId}/actions`,
    token
  );
}

export function runClean(
  token: string,
  jobId: string,
  payload: {
    candidate_ids: string[];
    dry_run: boolean;
    confirmation_phrase?: string;
  }
): Promise<ActionResult> {
  return apiRequest<ActionResult>(`/api/jobs/${jobId}/actions/clean`, token, {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function runArchive(
  token: string,
  jobId: string,
  payload: {
    candidate_ids: string[];
    dry_run: boolean;
    confirmation_phrase?: string;
    target_path?: string;
    external_path?: string;
  }
): Promise<ActionResult> {
  return apiRequest<ActionResult>(`/api/jobs/${jobId}/actions/archive`, token, {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function openJobEvents(
  token: string,
  jobId: string,
  onJob: (job: Job) => void
): EventSource {
  const source = new EventSource(
    `${API_BASE}/api/jobs/${jobId}/events?token=${encodeURIComponent(token)}`
  );
  source.addEventListener("progress", (event) => {
    const job = JSON.parse((event as MessageEvent).data) as Job;
    onJob(job);
    if (job.status === "completed" || job.status === "failed") source.close();
  });
  return source;
}

export function listProtectedFolders(token: string): Promise<ProtectedFolder[]> {
  return apiRequest<ProtectedFolder[]>("/api/protection/folders", token);
}

export function getProtectedFolder(
  token: string,
  folderId: string,
  query = "",
  limit = 100,
  offset = 0
): Promise<ProtectedFolderDetail> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (query) params.set("query", query);
  return apiRequest<ProtectedFolderDetail>(
    `/api/protection/folders/${folderId}?${params}`,
    token
  );
}

export function protectionPreflight(token: string): Promise<ProtectionPreflight> {
  return apiRequest<ProtectionPreflight>("/api/protection/preflight", token);
}

export function createProtectedFolder(
  token: string,
  payload: { name: string; source_path: string; required_copies: number }
): Promise<ProtectedFolderCreated> {
  return apiRequest<ProtectedFolderCreated>("/api/protection/folders", token, {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function updateProtectedFolder(
  token: string,
  folderId: string,
  payload: {
    name: string;
    required_copies: number;
    confirmation_phrase?: string;
  }
): Promise<ProtectedFolder> {
  return apiRequest<ProtectedFolder>(`/api/protection/folders/${folderId}`, token, {
    method: "PATCH",
    body: JSON.stringify(payload)
  });
}

export function deleteProtectedFolder(
  token: string,
  folderId: string,
  confirmationPhrase: string
): Promise<{ deleted: string; repositories_left_intact: number; keychain_warning?: string }> {
  return apiRequest(`/api/protection/folders/${folderId}`, token, {
    method: "DELETE",
    body: JSON.stringify({ confirmation_phrase: confirmationPhrase })
  });
}

export function confirmRecoveryKey(
  token: string,
  folderId: string,
  recoveryKey: string
): Promise<ProtectedFolder> {
  return apiRequest<ProtectedFolder>(
    `/api/protection/folders/${folderId}/confirm-recovery`,
    token,
    { method: "POST", body: JSON.stringify({ recovery_key: recoveryKey }) }
  );
}

export function listMountedVolumes(token: string): Promise<{ volumes: MountedVolume[] }> {
  return apiRequest<{ volumes: MountedVolume[] }>("/api/protection/volumes", token);
}

export function enrollProtectedDrive(
  token: string,
  folderId: string,
  mountPath: string
): Promise<DriveTarget> {
  return apiRequest<DriveTarget>(`/api/protection/folders/${folderId}/drives`, token, {
    method: "POST",
    body: JSON.stringify({ mount_path: mountPath })
  });
}

export type ProtectionOperationPayload =
  | { kind: "index" }
  | { kind: "backup"; drive_target_id: string }
  | { kind: "verify"; drive_target_id: string; full?: boolean; file_ids?: string[] }
  | {
      kind: "evict";
      file_ids?: string[];
      all_files?: boolean;
      dry_run: boolean;
      confirmation_phrase?: string;
    }
  | {
      kind: "restore";
      file_ids: string[];
      drive_target_id?: string;
      destination?: string;
    };

export function createProtectionOperation(
  token: string,
  folderId: string,
  payload: ProtectionOperationPayload
): Promise<ProtectionOperation> {
  return apiRequest<ProtectionOperation>(
    `/api/protection/folders/${folderId}/operations`,
    token,
    { method: "POST", body: JSON.stringify(payload) }
  );
}

export function cancelProtectionOperation(
  token: string,
  operationId: string
): Promise<ProtectionOperation> {
  return apiRequest<ProtectionOperation>(
    `/api/protection/operations/${operationId}/cancel`,
    token,
    { method: "POST" }
  );
}

export function openProtectionEvents(
  token: string,
  operationId: string,
  onOperation: (operation: ProtectionOperation) => void
): EventSource {
  const source = new EventSource(
    `${API_BASE}/api/protection/operations/${operationId}/events?token=${encodeURIComponent(token)}`
  );
  source.addEventListener("progress", (event) => {
    const operation = JSON.parse((event as MessageEvent).data) as ProtectionOperation;
    onOperation(operation);
    if (["completed", "failed", "cancelled"].includes(operation.status)) source.close();
  });
  return source;
}

export function importProtectedFolder(
  token: string,
  payload: {
    name: string;
    source_path: string;
    mount_path: string;
    recovery_key: string;
    required_copies: number;
    protected_folder_id?: string;
  }
): Promise<ProtectedFolderDetail> {
  return apiRequest<ProtectedFolderDetail>("/api/protection/import", token, {
    method: "POST",
    body: JSON.stringify(payload)
  });
}
