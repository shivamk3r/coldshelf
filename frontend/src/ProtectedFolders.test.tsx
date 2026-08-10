import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ProtectedFolders } from "./ProtectedFolders";
import * as api from "./api";

const folder = {
  id: "folder-1",
  name: "Large Files",
  source_path: "/Users/demo/large-files",
  required_copies: 2,
  recovery_confirmed: true,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z"
};

const drive = {
  id: "drive-1",
  protected_folder_id: folder.id,
  volume_uuid: "volume-1",
  volume_name: "HDD1",
  filesystem: "ExFAT",
  repo_relative_path: "DiskSpaceManager/protected/folder-1/restic",
  repository_id: "repository-identity-123",
  total_bytes: 2_000_000,
  free_bytes: 1_500_000,
  status: "online",
  mounted: true,
  enrolled_at: "2026-01-01T00:00:00Z",
  last_backup_at: "2026-01-02T00:00:00Z",
  last_structural_check_at: "2026-01-02T00:00:00Z"
};

const detail = {
  folder,
  summary: {
    file_count: 1,
    total_bytes: 1024,
    local_files: 1,
    local_bytes: 1024,
    pending_files: 1,
    pending_bytes: 1024,
    unprotected_files: 0,
    unprotected_bytes: 0,
    partial_files: 1,
    partial_bytes: 1024,
    protected_files: 0,
    protected_bytes: 0,
    evicted_files: 0,
    evicted_bytes: 0,
    at_risk_files: 0,
    at_risk_bytes: 0
  },
  drives: [drive],
  files: [
    {
      id: "file-1",
      protected_folder_id: folder.id,
      relative_path: "video.bin",
      file_type: "file",
      size: 1024,
      local_state: "local",
      verified_replica_count: 1,
      replicas: [
        {
          file_version_id: "file-1",
          drive_target_id: drive.id,
          generation_id: "generation-1",
          snapshot_id: "snapshot-1",
          status: "structural_checked",
          backed_up_at: "2026-01-02T00:00:00Z"
        }
      ]
    }
  ],
  operations: []
};

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    listProtectedFolders: vi.fn(),
    listMountedVolumes: vi.fn(),
    getProtectedFolder: vi.fn(),
    createProtectionOperation: vi.fn(),
    openProtectionEvents: vi.fn(() => ({ close: vi.fn(), addEventListener: vi.fn() }))
  };
});

beforeEach(() => {
  vi.mocked(api.listProtectedFolders).mockResolvedValue([folder]);
  vi.mocked(api.listMountedVolumes).mockResolvedValue({ volumes: [] });
  vi.mocked(api.getProtectedFolder).mockResolvedValue(detail);
  vi.mocked(api.createProtectionOperation).mockResolvedValue({
    id: "operation-1",
    protected_folder_id: folder.id,
    kind: "evict",
    status: "queued",
    request: { dry_run: true },
    progress_percent: 0,
    progress_message: "Queued",
    diagnostics: {},
    cancel_requested: false,
    created_at: "now"
  });
});

test("shows replica cards and sends catalog file IDs for bulk actions", async () => {
  render(<ProtectedFolders token="secret" />);
  expect(
    await screen.findByRole("heading", { name: "Large Files" })
  ).toBeInTheDocument();
  expect(await screen.findByText("repository-i…")).toBeInTheDocument();
  expect(screen.getByText("video.bin")).toBeInTheDocument();
  expect(screen.getByText("structural")).toBeInTheDocument();

  await userEvent.click(screen.getByLabelText("Select video.bin"));
  await userEvent.click(screen.getByRole("button", { name: "Preview selected eviction" }));
  expect(api.createProtectionOperation).toHaveBeenCalledWith(
    "secret",
    "folder-1",
    { kind: "evict", file_ids: ["file-1"], dry_run: true }
  );
});

test("polls mounted volumes every five seconds only while mounted", async () => {
  vi.useFakeTimers();
  const { unmount } = render(<ProtectedFolders token="secret" />);
  await act(async () => Promise.resolve());
  const initialCalls = vi.mocked(api.listMountedVolumes).mock.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(vi.mocked(api.listMountedVolumes).mock.calls.length).toBeGreaterThan(initialCalls);
  unmount();
  const callsAfterUnmount = vi.mocked(api.listMountedVolumes).mock.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(vi.mocked(api.listMountedVolumes).mock.calls.length).toBe(callsAfterUnmount);
  vi.useRealTimers();
});

test("shows Restic transfer ETA and clears it for the structural check", async () => {
  let onOperation: ((operation: Awaited<ReturnType<typeof api.createProtectionOperation>>) => void) | undefined;
  vi.mocked(api.createProtectionOperation).mockResolvedValueOnce({
    id: "operation-eta",
    protected_folder_id: folder.id,
    kind: "backup",
    status: "running",
    drive_target_id: drive.id,
    request: { kind: "backup", drive_target_id: drive.id },
    progress_percent: 40,
    progress_message: "Processing video.bin",
    eta_seconds: 125,
    diagnostics: {},
    cancel_requested: false,
    created_at: "now",
    started_at: "now"
  });
  vi.mocked(api.openProtectionEvents).mockImplementationOnce((_token, _operationId, callback) => {
    onOperation = callback;
    return { close: vi.fn() } as unknown as EventSource;
  });

  render(<ProtectedFolders token="secret" />);
  await screen.findByRole("heading", { name: "Large Files" });
  await userEvent.click(screen.getByRole("button", { name: "Back up" }));

  expect(await screen.findByText("About 2m 5s of file transfer remaining.")).toBeInTheDocument();
  act(() => {
    onOperation?.({
      id: "operation-eta",
      protected_folder_id: folder.id,
      kind: "backup",
      status: "running",
      drive_target_id: drive.id,
      request: { kind: "backup", drive_target_id: drive.id },
      progress_percent: 82,
      progress_message: "Checking repository structure",
      eta_seconds: null,
      diagnostics: {},
      cancel_requested: false,
      created_at: "now",
      started_at: "now"
    });
  });
  expect(screen.getByText("Checking repository structure")).toBeInTheDocument();
  expect(screen.queryByText(/file transfer remaining/)).not.toBeInTheDocument();
});
