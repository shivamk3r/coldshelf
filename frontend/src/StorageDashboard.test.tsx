import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  getActiveDockerAction, getDockerItems, getDockerUsage, getStorageSummary,
  getRecentDockerActions, previewDockerCleanup, scheduleDockerCleanup
} from "./api";
import { StorageDashboard } from "./StorageDashboard";

vi.mock("./api", () => ({
  getStorageSummary: vi.fn(),
  getDockerUsage: vi.fn(),
  getDockerItems: vi.fn(),
  getActiveDockerAction: vi.fn(),
  getRecentDockerActions: vi.fn(),
  previewDockerCleanup: vi.fn(),
  scheduleDockerCleanup: vi.fn(),
  getDockerAction: vi.fn(),
  undoDockerAction: vi.fn(),
  createBrowseScan: vi.fn(),
  getBrowseScan: vi.fn(),
  getBrowseResult: vi.fn(),
  cancelBrowseScan: vi.fn()
}));

const usage = {
  available: true, daemon_id: "daemon-1", same_filesystem: true,
  measured_at: "2026-09-24T12:00:00Z", root_path: "/var/lib/docker",
  groups: {
    images: { count: 1, active_count: 0, size_bytes: 1024, reclaimable_bytes: 512 },
    containers: { count: 0, active_count: 0, size_bytes: 0, reclaimable_bytes: 0 },
    volumes: { count: 1, active_count: 0, size_bytes: 2048, reclaimable_bytes: 2048 },
    cache: { count: 0, active_count: 0, size_bytes: 0, reclaimable_bytes: 0 }
  }
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getStorageSummary).mockResolvedValue({
    total_bytes: 100 * 1024 ** 3, used_bytes: 80 * 1024 ** 3,
    available_bytes: 20 * 1024 ** 3
  });
  vi.mocked(getDockerUsage).mockResolvedValue(usage);
  vi.mocked(getDockerItems).mockImplementation(async (_token, kind) => ({
    kind, page: 1, limit: 50, total: 1,
    items: kind === "volumes" ? [{ id: "data", name: "data", size_bytes: 2048,
      driver: "local", references: 0, eligible: true }] :
      [{ id: "sha256:abc", name: "demo:latest", size_bytes: 1024, unique_bytes: 512,
        references: 0, eligible: true }]
  }));
  vi.mocked(getActiveDockerAction).mockResolvedValue({ action: null });
  vi.mocked(getRecentDockerActions).mockResolvedValue({ actions: [] });
});

test("shows filesystem capacity and Docker as separate, non-additive usage", async () => {
  render(<StorageDashboard token="secret" onOpenBrowse={vi.fn()} />);
  expect(await screen.findByText(/80 GiB of 100 GiB used/)).toBeInTheDocument();
  expect(screen.getByText(/Docker usage can overlap/)).toBeInTheDocument();
  expect(screen.getByText(/Docker data is on this filesystem/)).toBeInTheDocument();
  expect(screen.getByText(/2.0 KiB reclaimable estimate/)).toBeInTheDocument();
});

test("volume cleanup needs a reviewed preview and exact typed phrase", async () => {
  vi.mocked(previewDockerCleanup).mockResolvedValue({
    id: "preview-1", kind: "volumes", ids: ["data"],
    items: [{ id: "data", name: "data", size_bytes: 2048 }],
    estimated_bytes: 2048, created_at: "now", expires_at: "later",
    confirmation_phrase: "DELETE 1 DOCKER VOLUMES"
  });
  vi.mocked(scheduleDockerCleanup).mockResolvedValue({
    id: "action-1", kind: "volumes", status: "scheduled", items: [], outcomes: [],
    estimated_bytes: 2048, created_at: "2026-09-24T12:00:00Z",
    delete_after: "2026-09-24T12:00:10Z", server_time: "2026-09-24T12:00:00Z",
    undo_available: true
  });
  const user = userEvent.setup();
  render(<StorageDashboard token="secret" onOpenBrowse={vi.fn()} />);
  await user.click(await screen.findByRole("button", { name: /Volumes.*2.0 KiB/ }));
  await user.click(await screen.findByRole("checkbox"));
  await user.click(screen.getByRole("button", { name: "Review selected volumes" }));
  expect(await screen.findByText(/Volume data will be permanently deleted/)).toBeInTheDocument();
  expect(scheduleDockerCleanup).not.toHaveBeenCalled();
  const confirm = screen.getByRole("button", { name: "Schedule cleanup" });
  expect(confirm).toBeDisabled();
  await user.type(screen.getByLabelText(/Type DELETE 1 DOCKER VOLUMES/), "DELETE 1 DOCKER VOLUMES");
  await user.click(confirm);
  await waitFor(() => expect(scheduleDockerCleanup).toHaveBeenCalledWith("secret", "preview-1", "DELETE 1 DOCKER VOLUMES"));
});

test("keeps filesystem capacity visible when Docker is unavailable", async () => {
  vi.mocked(getDockerUsage).mockResolvedValue({ available: false, message: "No local daemon", same_filesystem: false, groups: {} });
  render(<StorageDashboard token="secret" onOpenBrowse={vi.fn()} />);
  expect(await screen.findByText(/80 GiB of 100 GiB used/)).toBeInTheDocument();
  expect(screen.getByText("No local daemon")).toBeInTheDocument();
});
