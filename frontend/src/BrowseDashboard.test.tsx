import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { BrowseDashboard, clearBrowseCache } from "./BrowseDashboard";
import * as api from "./api";
import type { BrowseDeletion, BrowseDeletionPreview, BrowseEntry, BrowseResult, BrowseScan } from "./types";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    createBrowseScan: vi.fn(),
    getBrowseResult: vi.fn(),
    cancelBrowseScan: vi.fn(),
    openBrowseEvents: vi.fn(),
    listProtectedFolders: vi.fn(),
    previewBrowseDeletion: vi.fn(),
    scheduleBrowseDeletion: vi.fn()
  };
});

const scan: BrowseScan = {
  id: "browse-1",
  status: "completed",
  path: "/Users/demo",
  created_at: "2026-08-24T12:00:00Z",
  started_at: "2026-08-24T12:00:00Z",
  finished_at: "2026-08-24T12:00:01Z",
  progress_percent: 100,
  progress_message: "Sizing complete",
  files_scanned: 3,
  directories_scanned: 2,
  completed_entries: 2,
  total_entries: 2,
  cancel_requested: false
};

function entry(overrides: Partial<BrowseEntry>): BrowseEntry {
  return {
    name: "Project",
    path: "/Users/demo/Project",
    kind: "directory",
    hidden: false,
    readable: true,
    writable: true,
    symlink: false,
    mount_boundary: false,
    logical_bytes: 1000,
    allocated_bytes: 2048,
    allocation_fallback_count: 0,
    file_count: 2,
    directory_count: 1,
    error_count: 0,
    skipped_mount_count: 0,
    size_state: "complete",
    protection_eligible: true,
    deletion_eligible: true,
    ...overrides
  };
}

const entries: BrowseEntry[] = [
  entry({}),
  entry({
    name: "notes.txt",
    path: "/Users/demo/notes.txt",
    kind: "file",
    logical_bytes: 20,
    allocated_bytes: 4096,
    file_count: 1,
    directory_count: 0,
    protection_eligible: false,
    protection_blocker: "Only folders can be protected"
  }),
  entry({
    name: ".cache",
    path: "/Users/demo/.cache",
    hidden: true,
    logical_bytes: 50,
    allocated_bytes: 4096,
    file_count: 1
  })
];

const result: BrowseResult = {
  current_path: "/Users/demo",
  parent_path: "/Users",
  roots: [{ name: "Home", path: "/Users/demo", kind: "home", readable: true, symlink: false }],
  entries,
  logical_bytes: 1070,
  allocated_bytes: 10240,
  file_count: 4,
  directory_count: 2,
  hidden_entry_count: 1,
  hidden_logical_bytes: 50,
  hidden_allocated_bytes: 4096,
  allocation_fallback_count: 0,
  error_count: 0,
  errors: [],
  measured_at: "2026-08-24T12:00:01Z"
};

const deletionPreview: BrowseDeletionPreview = {
  id: "preview-1",
  expires_at: "2026-08-24T12:05:00Z",
  current_path: "/Users/demo",
  selected_count: 2,
  logical_bytes: 1020,
  allocated_bytes: 6144,
  can_schedule: true,
  items: [
    { path: "/Users/demo/Project", name: "Project", kind: "directory", logical_bytes: 1000, allocated_bytes: 2048, status: "pending" },
    { path: "/Users/demo/notes.txt", name: "notes.txt", kind: "file", logical_bytes: 20, allocated_bytes: 4096, status: "pending" }
  ],
  blockers: []
};

const scheduledDeletion: BrowseDeletion = {
  id: "deletion-1",
  status: "scheduled",
  current_path: "/Users/demo",
  selected_count: 2,
  logical_bytes: 1020,
  allocated_bytes: 6144,
  created_at: "2026-08-24T12:00:00Z",
  delete_after: "2026-08-24T12:00:10Z",
  server_time: "2026-08-24T12:00:00Z",
  undo_available: true,
  items: deletionPreview.items
};

beforeEach(() => {
  clearBrowseCache();
  vi.clearAllMocks();
  vi.mocked(api.listProtectedFolders).mockResolvedValue([]);
  vi.mocked(api.createBrowseScan).mockResolvedValue({
    scan,
    listing: {
      current_path: result.current_path,
      parent_path: result.parent_path,
      roots: result.roots,
      entries
    }
  });
  vi.mocked(api.getBrowseResult).mockResolvedValue(result);
  vi.mocked(api.previewBrowseDeletion).mockResolvedValue(deletionPreview);
  vi.mocked(api.scheduleBrowseDeletion).mockResolvedValue(scheduledDeletion);
});

test("selects the current page, reviews the exact items, and schedules deletion", async () => {
  const onDeletionScheduled = vi.fn();
  render(
    <BrowseDashboard
      token="secret"
      onPathChanged={vi.fn()}
      onProtect={vi.fn()}
      onOpenProtected={vi.fn()}
      onOpenHelp={vi.fn()}
      onDeletionScheduled={onDeletionScheduled}
    />
  );

  await screen.findByText("10 KB");
  await userEvent.click(screen.getByRole("checkbox", { name: "Select eligible items on this page" }));
  expect(screen.getByText("2 selected")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Delete Permanently" }));

  await waitFor(() => expect(api.previewBrowseDeletion).toHaveBeenCalledTimes(1));
  const [token, scanId, paths] = vi.mocked(api.previewBrowseDeletion).mock.calls[0];
  expect(token).toBe("secret");
  expect(scanId).toBe(scan.id);
  expect(paths).toHaveLength(2);
  expect(paths).toEqual(expect.arrayContaining([
    "/Users/demo/Project",
    "/Users/demo/notes.txt"
  ]));
  expect(screen.getByRole("dialog", { name: "Review the exact selection" })).toBeInTheDocument();
  expect(screen.getByText("This does not use Trash.")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "Schedule permanent deletion" }));
  expect(api.scheduleBrowseDeletion).toHaveBeenCalledWith("secret", deletionPreview.id);
  expect(onDeletionScheduled).toHaveBeenCalledWith(scheduledDeletion);
});

test("limits bulk selection to the current 50-row page and resets it on refresh", async () => {
  const manyEntries = Array.from({ length: 51 }, (_, index) => entry({
    name: `delete-${String(index).padStart(3, "0")}.txt`,
    path: `/Users/demo/delete-${String(index).padStart(3, "0")}.txt`,
    kind: "file",
    file_count: 1,
    directory_count: 0,
    protection_eligible: false,
    protection_blocker: "Only folders can be protected"
  }));
  const manyResult = {
    ...result,
    entries: manyEntries,
    hidden_entry_count: 0,
    hidden_logical_bytes: 0,
    hidden_allocated_bytes: 0
  };
  vi.mocked(api.createBrowseScan).mockResolvedValue({
    scan,
    listing: {
      current_path: manyResult.current_path,
      parent_path: manyResult.parent_path,
      roots: manyResult.roots,
      entries: manyEntries
    }
  });
  vi.mocked(api.getBrowseResult).mockResolvedValue(manyResult);

  render(
    <BrowseDashboard
      token="secret"
      onPathChanged={vi.fn()}
      onProtect={vi.fn()}
      onOpenProtected={vi.fn()}
      onOpenHelp={vi.fn()}
    />
  );

  await screen.findByText("Showing 1–50 of 51");
  await userEvent.click(screen.getByRole("checkbox", { name: "Select eligible items on this page" }));
  expect(screen.getByText("50 selected")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Next" }));
  await userEvent.click(screen.getByRole("checkbox", { name: "Select eligible items on this page" }));
  expect(screen.getByText("51 selected")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await waitFor(() => expect(screen.queryByText("51 selected")).not.toBeInTheDocument());
});

test("shows server blockers and API errors without enabling confirmation", async () => {
  vi.mocked(api.previewBrowseDeletion).mockResolvedValueOnce({
    ...deletionPreview,
    selected_count: 0,
    logical_bytes: 0,
    allocated_bytes: 0,
    can_schedule: false,
    items: [],
    blockers: [{ path: "/Users/demo/notes.txt", reason: "The item changed after it was scanned" }]
  });
  render(
    <BrowseDashboard
      token="secret"
      onPathChanged={vi.fn()}
      onProtect={vi.fn()}
      onOpenProtected={vi.fn()}
      onOpenHelp={vi.fn()}
    />
  );

  await screen.findByText("10 KB");
  await userEvent.click(screen.getByRole("checkbox", { name: "Select notes.txt for permanent deletion" }));
  await userEvent.click(screen.getByRole("button", { name: "Delete Permanently" }));
  expect(await screen.findByText("The item changed after it was scanned")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Schedule permanent deletion" })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Close deletion review" }));

  vi.mocked(api.previewBrowseDeletion).mockRejectedValueOnce(new Error("Preview expired; refresh Browse"));
  await userEvent.click(screen.getByRole("button", { name: "Delete Permanently" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Preview expired; refresh Browse");
});

test("keeps scheduled rows visible but disables their actions", async () => {
  render(
    <BrowseDashboard
      token="secret"
      onPathChanged={vi.fn()}
      onProtect={vi.fn()}
      onOpenProtected={vi.fn()}
      onOpenHelp={vi.fn()}
      activeDeletion={{ ...scheduledDeletion, selected_count: 1, items: [scheduledDeletion.items[0]] }}
    />
  );

  expect(await screen.findByText("10 KB")).toBeInTheDocument();
  expect(screen.getAllByText("pending deletion").length).toBeGreaterThan(0);
  expect(screen.getByRole("checkbox", { name: "Select Project for permanent deletion" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Project" })).not.toBeInTheDocument();
  expect(screen.getByText("notes.txt")).toBeInTheDocument();
});

test("shows one-level sizes, keeps hidden bytes in totals, and starts protection", async () => {
  const onProtect = vi.fn();
  render(
    <BrowseDashboard
      token="secret"
      onPathChanged={vi.fn()}
      onProtect={onProtect}
      onOpenProtected={vi.fn()}
      onOpenHelp={vi.fn()}
    />
  );

  expect(await screen.findByText("10 KB")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Project" })).toBeInTheDocument();
  expect(screen.getByText("notes.txt")).toBeInTheDocument();
  expect(screen.queryByText(".cache")).not.toBeInTheDocument();
  expect(screen.getByText(/1 hidden item uses 4.0 KB/)).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "Protect & offload" }));
  expect(onProtect).toHaveBeenCalledWith({
    name: "Project",
    sourcePath: "/Users/demo/Project",
    returnPath: "/Users/demo"
  });

  await userEvent.click(screen.getByRole("button", { name: "Show them" }));
  expect(screen.getByText(".cache")).toBeInTheDocument();
});

test("opens an exactly registered protected folder instead of creating an overlap", async () => {
  vi.mocked(api.listProtectedFolders).mockResolvedValue([{
    id: "folder-1",
    name: "Project",
    source_path: "/Users/demo/Project",
    required_copies: 2,
    recovery_confirmed: true,
    created_at: "now",
    updated_at: "now"
  }]);
  const onOpenProtected = vi.fn();
  render(
    <BrowseDashboard
      token="secret"
      onPathChanged={vi.fn()}
      onProtect={vi.fn()}
      onOpenProtected={onOpenProtected}
      onOpenHelp={vi.fn()}
    />
  );

  await waitFor(() => expect(screen.getByRole("button", { name: "Open protected folder" })).toBeInTheDocument());
  await userEvent.click(screen.getByRole("button", { name: "Open protected folder" }));
  expect(onOpenProtected).toHaveBeenCalledWith("folder-1", "/Users/demo");
});

test("uses cached back-navigation and makes Refresh bypass the cache", async () => {
  const nestedScan = { ...scan, id: "browse-2", path: "/Users/demo/Project" };
  const nestedResult: BrowseResult = {
    ...result,
    current_path: "/Users/demo/Project",
    parent_path: "/Users/demo",
    entries: [],
    logical_bytes: 0,
    allocated_bytes: 0,
    file_count: 0,
    directory_count: 0,
    hidden_entry_count: 0,
    hidden_logical_bytes: 0,
    hidden_allocated_bytes: 0
  };
  vi.mocked(api.createBrowseScan).mockImplementation(async (_token, path) => {
    const nested = path === nestedResult.current_path;
    const next = nested ? nestedResult : result;
    return {
      scan: nested ? nestedScan : scan,
      listing: {
        current_path: next.current_path,
        parent_path: next.parent_path,
        roots: next.roots,
        entries: next.entries
      }
    };
  });
  vi.mocked(api.getBrowseResult).mockImplementation(async (_token, scanId) => (
    scanId === nestedScan.id ? nestedResult : result
  ));

  render(
    <BrowseDashboard
      token="secret"
      onPathChanged={vi.fn()}
      onProtect={vi.fn()}
      onOpenProtected={vi.fn()}
      onOpenHelp={vi.fn()}
    />
  );

  await userEvent.click(await screen.findByRole("button", { name: "Project" }));
  await waitFor(() => expect(screen.getByLabelText("Folder path")).toHaveValue("/Users/demo/Project"));
  expect(api.createBrowseScan).toHaveBeenCalledTimes(2);

  await userEvent.click(screen.getByRole("button", { name: "Open parent folder" }));
  await waitFor(() => expect(screen.getByLabelText("Folder path")).toHaveValue("/Users/demo"));
  expect(api.createBrowseScan).toHaveBeenCalledTimes(2);

  await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await waitFor(() => expect(api.createBrowseScan).toHaveBeenCalledTimes(3));
});

test("sorts, filters, and paginates a large one-level listing", async () => {
  const manyEntries = Array.from({ length: 51 }, (_, index) => entry({
    name: `item-${String(index).padStart(3, "0")}`,
    path: `/Users/demo/item-${String(index).padStart(3, "0")}`,
    kind: "file",
    logical_bytes: 100,
    allocated_bytes: 4096,
    file_count: 1,
    directory_count: 0,
    protection_eligible: false,
    protection_blocker: "Only folders can be protected"
  }));
  const manyResult = {
    ...result,
    entries: manyEntries,
    file_count: manyEntries.length,
    directory_count: 0,
    hidden_entry_count: 0,
    hidden_logical_bytes: 0,
    hidden_allocated_bytes: 0
  };
  vi.mocked(api.createBrowseScan).mockResolvedValue({
    scan,
    listing: {
      current_path: manyResult.current_path,
      parent_path: manyResult.parent_path,
      roots: manyResult.roots,
      entries: manyEntries
    }
  });
  vi.mocked(api.getBrowseResult).mockResolvedValue(manyResult);
  const { container } = render(
    <BrowseDashboard
      token="secret"
      onPathChanged={vi.fn()}
      onProtect={vi.fn()}
      onOpenProtected={vi.fn()}
      onOpenHelp={vi.fn()}
    />
  );

  expect(await screen.findByText("Showing 1–50 of 51")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Next" }));
  expect(screen.getByText("item-000")).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "Name" }));
  expect(container.querySelector("tbody tr")?.textContent).toContain("item-000");

  await userEvent.type(screen.getByPlaceholderText("Filter this level"), "item-050");
  expect(screen.getByText("Showing 1–1 of 1")).toBeInTheDocument();
  expect(screen.getByText("item-050")).toBeInTheDocument();
});

test("cancels running sizing and keeps initial direct-file metadata visible", async () => {
  const running = {
    ...scan,
    status: "running" as const,
    finished_at: null,
    progress_percent: 20,
    progress_message: "Scanned 1 file",
    completed_entries: 0
  };
  let sendScan: ((next: BrowseScan) => void) | undefined;
  vi.mocked(api.createBrowseScan).mockResolvedValue({
    scan: running,
    listing: {
      current_path: result.current_path,
      parent_path: result.parent_path,
      roots: result.roots,
      entries
    }
  });
  vi.mocked(api.openBrowseEvents).mockImplementation((_token, _id, callback) => {
    sendScan = callback;
    return { close: vi.fn() } as unknown as EventSource;
  });
  vi.mocked(api.cancelBrowseScan).mockResolvedValue({
    ...running,
    cancel_requested: true,
    progress_message: "Cancellation requested"
  });

  render(
    <BrowseDashboard
      token="secret"
      onPathChanged={vi.fn()}
      onProtect={vi.fn()}
      onOpenProtected={vi.fn()}
      onOpenHelp={vi.fn()}
    />
  );

  expect(await screen.findByText("notes.txt")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(api.cancelBrowseScan).toHaveBeenCalledWith("secret", running.id);
  act(() => sendScan?.({ ...running, status: "cancelled", cancel_requested: true }));
  expect(screen.getByText("Sizing was cancelled.")).toBeInTheDocument();
});

test("blocks new protection when protected-folder status cannot be verified", async () => {
  vi.mocked(api.listProtectedFolders).mockRejectedValue(new Error("Catalog unavailable"));

  render(
    <BrowseDashboard
      token="secret"
      onPathChanged={vi.fn()}
      onProtect={vi.fn()}
      onOpenProtected={vi.fn()}
      onOpenHelp={vi.fn()}
    />
  );

  expect(await screen.findByText("Protected-folder status unavailable")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Protect & offload" })).not.toBeInTheDocument();
});

test("explains why a symbolic link cannot start protected offload", async () => {
  const symlink = entry({
    name: "Project shortcut",
    path: "/Users/demo/Project shortcut",
    kind: "symlink",
    symlink: true,
    logical_bytes: 7,
    allocated_bytes: 4096,
    file_count: 0,
    directory_count: 0,
    protection_eligible: false,
    protection_blocker: "Folder shortcuts are not followed or protectable"
  });
  vi.mocked(api.createBrowseScan).mockResolvedValue({
    scan,
    listing: { ...result, entries: [symlink] }
  });
  vi.mocked(api.getBrowseResult).mockResolvedValue({ ...result, entries: [symlink] });

  render(
    <BrowseDashboard
      token="secret"
      onPathChanged={vi.fn()}
      onProtect={vi.fn()}
      onOpenProtected={vi.fn()}
      onOpenHelp={vi.fn()}
    />
  );

  expect(await screen.findByText("Folder shortcuts are not followed or protectable")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Protect & offload" })).not.toBeInTheDocument();
});
