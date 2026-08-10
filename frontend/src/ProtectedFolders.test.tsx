import { act, render, screen, waitFor } from "@testing-library/react";
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
    protectionPreflight: vi.fn(),
    getProtectedFolder: vi.fn(),
    createProtectionOperation: vi.fn(),
    cancelProtectionOperation: vi.fn(),
    updateProtectedFolder: vi.fn(),
    deleteProtectedFolder: vi.fn(),
    importProtectedFolder: vi.fn(),
    openProtectionEvents: vi.fn(() => ({ close: vi.fn(), addEventListener: vi.fn() }))
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.protectionPreflight).mockResolvedValue({
    platform: "Darwin",
    supported: true,
    restic_version: "0.19.1",
    minimum_restic_version: "0.19.1",
    ready: true,
    message: "Protected folders are ready."
  });
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
  vi.mocked(api.cancelProtectionOperation).mockResolvedValue({
    id: "operation-1",
    protected_folder_id: folder.id,
    kind: "backup",
    status: "running",
    request: {},
    progress_percent: 10,
    progress_message: "Cancellation requested",
    diagnostics: {},
    cancel_requested: true,
    created_at: "now"
  });
  vi.mocked(api.updateProtectedFolder).mockResolvedValue(folder);
  vi.mocked(api.deleteProtectedFolder).mockResolvedValue({
    deleted: folder.id,
    repositories_left_intact: 1
  });
  vi.mocked(api.importProtectedFolder).mockResolvedValue(detail);
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

test("guards lower-copy settings and catalog deletion with their exact phrases", async () => {
  render(<ProtectedFolders token="secret" />);
  await screen.findByRole("heading", { name: "Large Files" });
  await userEvent.click(screen.getByRole("button", { name: "Settings" }));

  const copies = screen.getByLabelText("Required verified copies");
  await userEvent.clear(copies);
  await userEvent.type(copies, "1");
  const save = screen.getByRole("button", { name: "Save settings" });
  expect(save).toBeDisabled();
  await userEvent.type(
    screen.getByLabelText("Lower copy count confirmation"),
    "UPDATE COPIES Large Files 1"
  );
  await userEvent.click(save);
  expect(api.updateProtectedFolder).toHaveBeenCalledWith("secret", folder.id, {
    name: folder.name,
    required_copies: 1,
    confirmation_phrase: "UPDATE COPIES Large Files 1"
  });

  await userEvent.click(await screen.findByRole("button", { name: "Settings" }));
  await userEvent.click(screen.getByText("Delete protected catalog"));
  const deleteButton = screen.getByRole("button", { name: "Delete catalog only" });
  expect(deleteButton).toBeDisabled();
  await userEvent.type(
    screen.getByLabelText("Delete protected catalog confirmation"),
    "DELETE PROTECTED Large Files"
  );
  await userEvent.click(deleteButton);
  expect(api.deleteProtectedFolder).toHaveBeenCalledWith(
    "secret",
    folder.id,
    "DELETE PROTECTED Large Files"
  );
});

test("runs selected full verification and restores to an explicit destination", async () => {
  const { unmount } = render(<ProtectedFolders token="secret" />);
  await screen.findByRole("heading", { name: "Large Files" });
  await userEvent.click(screen.getByLabelText("Select video.bin"));
  await userEvent.selectOptions(
    screen.getByLabelText("Drive for selected full verification"),
    drive.id
  );
  await userEvent.click(screen.getByRole("button", { name: "Full verify selected" }));
  expect(api.createProtectionOperation).toHaveBeenCalledWith("secret", folder.id, {
    kind: "verify",
    drive_target_id: drive.id,
    full: true,
    file_ids: ["file-1"]
  });
  unmount();

  render(<ProtectedFolders token="secret" />);
  await screen.findByRole("heading", { name: "Large Files" });
  await userEvent.click(screen.getByLabelText("Select video.bin"));
  await userEvent.click(screen.getByRole("button", { name: "Restore selected" }));
  await userEvent.click(screen.getByRole("button", { name: "Another folder" }));
  await userEvent.type(screen.getByLabelText("Alternate destination"), "/tmp/restored");
  await userEvent.click(screen.getByRole("button", { name: "Start safe restore" }));
  expect(api.createProtectionOperation).toHaveBeenLastCalledWith("secret", folder.id, {
    kind: "restore",
    file_ids: ["file-1"],
    destination: "/tmp/restored"
  });
});

test("imports recovery catalogs with the chosen required-copy count", async () => {
  const mounted = {
    volume_uuid: "volume-2",
    name: "HDD2",
    mount_path: "/Volumes/HDD2",
    filesystem: "ExFAT",
    device_identifier: "disk9",
    total_bytes: 2_000_000,
    free_bytes: 1_000_000,
    writable: true,
    internal: false,
    removable: true,
    enrolled: false,
    drive_target_id: null
  };
  vi.mocked(api.listMountedVolumes).mockResolvedValue({ volumes: [mounted] });
  render(<ProtectedFolders token="secret" intent="recover" />);

  expect(await screen.findByRole("heading", { name: "Fresh-Mac recovery import" })).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText("Future restore path"), "/tmp/future");
  await userEvent.selectOptions(screen.getByLabelText("Connected enrolled disk"), mounted.mount_path);
  const copies = screen.getByLabelText("Required independent copies");
  await userEvent.clear(copies);
  await userEvent.type(copies, "3");
  await userEvent.type(screen.getByLabelText("Recovery key"), "saved-key");
  await userEvent.click(screen.getByRole("button", { name: "Import catalog" }));

  await waitFor(() => expect(api.importProtectedFolder).toHaveBeenCalledWith("secret", {
    name: "Recovered protected folder",
    source_path: "/tmp/future",
    mount_path: mounted.mount_path,
    recovery_key: "saved-key",
    required_copies: 3,
    protected_folder_id: undefined
  }));
});

test("requests the next catalog page and reconnects to a running operation", async () => {
  const running = {
    id: "running-operation",
    protected_folder_id: folder.id,
    kind: "backup" as const,
    status: "running" as const,
    drive_target_id: drive.id,
    request: { kind: "backup", drive_target_id: drive.id },
    progress_percent: 20,
    progress_message: "Backing up",
    diagnostics: {},
    cancel_requested: false,
    created_at: "now"
  };
  vi.mocked(api.getProtectedFolder).mockResolvedValue({
    ...detail,
    catalog: { query: "", limit: 100, offset: 0, total: 101 },
    operations: [running]
  });
  render(<ProtectedFolders token="secret" />);

  await screen.findByText("Showing 1–1 of 101");
  expect(api.openProtectionEvents).toHaveBeenCalledWith(
    "secret",
    running.id,
    expect.any(Function)
  );
  await userEvent.click(screen.getByRole("button", { name: "Next" }));
  await waitFor(() => expect(api.getProtectedFolder).toHaveBeenCalledWith(
    "secret",
    folder.id,
    "",
    100,
    100
  ));
  await userEvent.click(screen.getByRole("button", { name: "Cancel safely" }));
  expect(api.cancelProtectionOperation).toHaveBeenCalledWith("secret", running.id);
});
