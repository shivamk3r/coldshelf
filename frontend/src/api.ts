import type {
  ActionResult,
  DriveTarget,
  Job,
  MountedVolume,
  ProtectedFolder,
  ProtectedFolderCreated,
  ProtectedFolderDetail,
  ProtectionOperation,
  Report
} from "./types";

const API_BASE = import.meta.env.VITE_DSM_API_BASE || "";
const ENV_TOKEN = import.meta.env.VITE_DSM_TOKEN || "";

export function initialToken(): string {
  const params = new URLSearchParams(window.location.search);
  return params.get("token") || ENV_TOKEN || localStorage.getItem("dsm-token") || "";
}

export function saveToken(token: string): void {
  localStorage.setItem("dsm-token", token);
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
    throw new Error(message);
  }
  return response.json() as Promise<T>;
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
    onJob(JSON.parse((event as MessageEvent).data) as Job);
  });
  return source;
}

export function listProtectedFolders(token: string): Promise<ProtectedFolder[]> {
  return apiRequest<ProtectedFolder[]>("/api/protection/folders", token);
}

export function getProtectedFolder(
  token: string,
  folderId: string,
  query = ""
): Promise<ProtectedFolderDetail> {
  const suffix = query ? `?query=${encodeURIComponent(query)}` : "";
  return apiRequest<ProtectedFolderDetail>(
    `/api/protection/folders/${folderId}${suffix}`,
    token
  );
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
    onOperation(JSON.parse((event as MessageEvent).data) as ProtectionOperation);
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
