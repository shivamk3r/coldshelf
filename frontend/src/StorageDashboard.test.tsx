import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  getActiveDockerAction, getDockerItems, getDockerUsage, getStorageSummary,
  getRecentDockerActions, previewDockerCleanup, scheduleDockerCleanup
} from "./api";
import { StorageDashboard } from "./StorageDashboard";
import type { DockerPreview } from "./types";

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

const cachePreview: DockerPreview = {
  id: "cache-preview", kind: "cache", ids: ["root", "middle", "leaf", "shared"],
  items: ["root", "middle", "leaf", "shared"].map((id) => ({ id, size_bytes: 1024 })),
  older_than_days: 7, cutoff: "2026-09-22T12:00:00Z", estimated_bytes: 2048,
  cache_summary: {
    matching_count: 6, matching_bytes: 8192, removable_count: 4, removable_bytes: 4096,
    private_bytes: 2048, shared_bytes: 2048, unknown_shared_bytes: 0,
    retained_count: 2, retained_bytes: 4096
  },
  created_at: "now", expires_at: "later", confirmation_phrase: "REMOVE 4 DOCKER CACHE"
};

test("cache preview separates retained and shared bytes and changing age clears review", async () => {
  vi.mocked(previewDockerCleanup).mockResolvedValue(cachePreview);
  const user = userEvent.setup();
  render(<StorageDashboard token="secret" onOpenBrowse={vi.fn()} />);
  await user.click(await screen.findByRole("button", { name: /Build cache/ }));
  const age = screen.getByRole("combobox");
  expect(Array.from((age as HTMLSelectElement).options).map((option) => option.value)).toEqual(["7", "30", "90"]);
  await user.selectOptions(age, "7");
  await user.click(screen.getByRole("button", { name: "Review cache cleanup" }));
  expect(await screen.findByText(/4 cache records selected · unused for over 7 days/)).toBeInTheDocument();
  expect(previewDockerCleanup).toHaveBeenCalledWith("secret", "cache", [], 7, false);
  expect(screen.getByText(/6 age-matching records · 8.0 KiB total record size/)).toBeInTheDocument();
  expect(screen.getByText(/2 records \(4.0 KiB\) retained by cache outside this selection/)).toBeInTheDocument();
  expect(screen.getByText(/2.0 KiB private · 2.0 KiB shared with images/)).toBeInTheDocument();
  expect(screen.getByText(/Private cache size is a potential saving/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Schedule cleanup" })).toBeDisabled();
  await user.type(screen.getByLabelText(/Type REMOVE 4 DOCKER CACHE/), "REMOVE 4 DOCKER CACHE");
  expect(screen.getByRole("button", { name: "Schedule cleanup" })).toBeEnabled();
  await user.selectOptions(age, "90");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(scheduleDockerCleanup).not.toHaveBeenCalled();
});

test("all-retained cache review explains zero candidates without offering cleanup", async () => {
  vi.mocked(previewDockerCleanup).mockResolvedValue({
    ...cachePreview, items: [], ids: [], estimated_bytes: 0,
    cache_summary: { ...cachePreview.cache_summary!, removable_count: 0, removable_bytes: 0,
      private_bytes: 0, shared_bytes: 0, retained_count: 6, retained_bytes: 8192 }
  });
  const user = userEvent.setup();
  render(<StorageDashboard token="secret" onOpenBrowse={vi.fn()} />);
  await user.click(await screen.findByRole("button", { name: /Build cache/ }));
  await user.click(screen.getByRole("button", { name: "Review cache cleanup" }));
  expect(await screen.findByText(/No cache records can currently be removed/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Schedule cleanup" })).not.toBeInTheDocument();
  expect(screen.queryByLabelText(/Type REMOVE/)).not.toBeInTheDocument();
  expect(scheduleDockerCleanup).not.toHaveBeenCalled();
});

test("cache completion and history display actual counts and daemon accounting", async () => {
  const action = {
    id: "cache-action", kind: "cache" as const, status: "completed" as const,
    items: cachePreview.items, estimated_bytes: 2048,
    outcomes: [{ id: "build-cache", status: "deleted", deleted_count: 3,
      not_removed_count: 1, reclaimed_bytes: 1024, error: "Docker retained some reviewed records." }],
    created_at: "2026-09-24T12:00:00Z", delete_after: "2026-09-24T12:00:10Z",
    server_time: "2026-09-24T12:00:30Z", undo_available: false
  };
  vi.mocked(getActiveDockerAction).mockResolvedValue({ action });
  vi.mocked(getRecentDockerActions).mockResolvedValue({ actions: [action] });
  render(<StorageDashboard token="secret" onOpenBrowse={vi.fn()} />);
  await screen.findByText("Docker cache: completed");
  expect(screen.getAllByText(/3 cache records removed; 1 retained by Docker/)).toHaveLength(2);
  expect(screen.getAllByText(/Docker-reported reclaimed: 1.0 KiB. Filesystem free-space change may differ/)).toHaveLength(2);
});
