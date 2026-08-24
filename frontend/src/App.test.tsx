import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { getStorageSummary } from "./api";
import { App } from "./App";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    initialToken: () => "secret",
    saveToken: vi.fn(),
    getConfig: vi.fn().mockResolvedValue({
      default_scan_path: "/tmp",
      default_age_months: 6,
      max_candidate_rows: 5000,
      platform: "Darwin"
    }),
    getStorageSummary: vi.fn(),
    listJobs: vi.fn().mockResolvedValue({ jobs: [] }),
    getActiveBrowseDeletion: vi.fn().mockResolvedValue({ deletion: null }),
    createJob: vi.fn().mockResolvedValue({
      id: "job-1",
      status: "queued",
      path: "/tmp/demo",
      age_months: 6,
      include_duplicates: true,
      include_near_duplicates: true,
      created_at: "now",
      progress_phase: "queued",
      progress_percent: 0,
      progress_message: "Queued",
      confirmation_phrases: { clean: "CLEAN job-1", archive: "ARCHIVE job-1" }
    }),
    openJobEvents: vi.fn(() => ({ close: vi.fn(), addEventListener: vi.fn() })),
    listProtectedFolders: vi.fn().mockResolvedValue([]),
    createBrowseScan: vi.fn().mockResolvedValue({
      scan: {
        id: "browse-1",
        status: "completed",
        path: "/Users/demo",
        created_at: "now",
        started_at: "now",
        finished_at: "now",
        progress_percent: 100,
        progress_message: "Sizing complete",
        files_scanned: 0,
        directories_scanned: 0,
        completed_entries: 0,
        total_entries: 0,
        cancel_requested: false
      },
      listing: {
        current_path: "/Users/demo",
        parent_path: "/Users",
        roots: [],
        entries: []
      }
    }),
    getBrowseResult: vi.fn().mockResolvedValue({
      current_path: "/Users/demo",
      parent_path: "/Users",
      roots: [],
      entries: [],
      logical_bytes: 0,
      allocated_bytes: 0,
      file_count: 0,
      directory_count: 0,
      hidden_entry_count: 0,
      hidden_logical_bytes: 0,
      hidden_allocated_bytes: 0,
      allocation_fallback_count: 0,
      error_count: 0,
      errors: [],
      measured_at: "now"
    }),
    cancelBrowseScan: vi.fn(),
    openBrowseEvents: vi.fn(() => ({ close: vi.fn(), addEventListener: vi.fn() }))
  };
});

const storage = {
  total_bytes: 1024 ** 4,
  used_bytes: 768 * 1024 ** 3,
  available_bytes: 256 * 1024 ** 3
};

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  vi.clearAllMocks();
  vi.mocked(getStorageSummary).mockResolvedValue(storage);
});

test("shows a loading state while primary storage is being read", async () => {
  vi.mocked(getStorageSummary).mockReturnValueOnce(new Promise(() => {}));

  render(<App />);

  expect(await screen.findByRole("status")).toHaveTextContent("Reading primary storage");
});

test("shows primary storage capacity with an accessible usage meter", async () => {
  render(<App />);

  expect(await screen.findByText("768 GB of 1.0 TB used")).toBeInTheDocument();
  expect(screen.getByText("75% used")).toBeInTheDocument();
  expect(screen.getAllByText("256 GB")).toHaveLength(2);
  expect(screen.getByRole("progressbar", { name: "Primary storage used" })).toHaveAttribute(
    "aria-valuenow",
    "75"
  );
});

test("starts a scan job from the dashboard", async () => {
  render(<App />);
  await userEvent.click(await screen.findByRole("button", { name: "Analyze storage" }));
  const path = await screen.findByLabelText("Folder to scan");
  await userEvent.clear(path);
  await userEvent.type(path, "/tmp/demo");
  await userEvent.click(screen.getByRole("button", { name: "Start scan" }));
  expect(await screen.findByText("/tmp/demo")).toBeInTheDocument();
});

test("opens the archive workflow from the storage summary", async () => {
  render(<App />);

  await userEvent.click(await screen.findByRole("button", { name: "Archive old files" }));

  expect(await screen.findByText("Find old files to archive")).toBeInTheDocument();
});

test("can refresh the storage snapshot", async () => {
  render(<App />);
  await screen.findByText("768 GB of 1.0 TB used");

  await userEvent.click(screen.getByRole("button", { name: "Refresh storage summary" }));

  expect(await screen.findByText("768 GB of 1.0 TB used")).toBeInTheDocument();
  expect(getStorageSummary).toHaveBeenCalledTimes(2);
});

test("keeps Home usable and retries when storage lookup fails", async () => {
  vi.mocked(getStorageSummary)
    .mockRejectedValueOnce(new Error("Storage information is temporarily unavailable."))
    .mockResolvedValueOnce(storage);
  render(<App />);

  expect(await screen.findByRole("alert")).toHaveTextContent("Storage information is unavailable.");
  expect(screen.getByRole("button", { name: /Clean cache files/ })).toBeEnabled();

  await userEvent.click(screen.getByRole("button", { name: "Retry storage summary" }));

  expect(await screen.findByText("768 GB of 1.0 TB used")).toBeInTheDocument();
  expect(getStorageSummary).toHaveBeenCalledTimes(2);
});

test("opens Browse from the five-item sidebar and updates hash navigation", async () => {
  render(<App />);

  const navigation = await screen.findByRole("navigation", { name: "Dashboard" });
  expect(navigation.querySelectorAll("button")).toHaveLength(5);
  await userEvent.click(screen.getByRole("button", { name: "Browse" }));

  expect(await screen.findByRole("heading", { name: "Browse storage" })).toBeInTheDocument();
  expect(window.location.hash).toBe("#/browse");
  expect(screen.getByRole("button", { name: "Browse" })).toHaveAttribute("aria-current", "page");
});

test("restores Browse directly from its hash route", async () => {
  window.history.replaceState({}, "", "/#/browse");

  render(<App />);

  expect(await screen.findByRole("heading", { name: "Browse storage" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Browse" })).toHaveAttribute("aria-current", "page");
});
