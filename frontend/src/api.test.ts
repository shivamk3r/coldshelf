import { clearToken, getStorageSummary, initialToken, openBrowseEvents, openJobEvents } from "./api";


test("captures the startup token and immediately removes it from the visible URL", () => {
  window.history.replaceState({}, "", "/?keep=yes&token=private-value#/help");
  expect(initialToken()).toBe("private-value");
  expect(window.location.search).toBe("?keep=yes");
  expect(window.location.hash).toBe("#/help");
});


test("disconnect clears the persisted token", () => {
  localStorage.setItem("dsm-token", "private-value");
  clearToken();
  expect(localStorage.getItem("dsm-token")).toBeNull();
});


test("requests the authenticated primary storage summary", async () => {
  const payload = { total_bytes: 1000, used_bytes: 600, available_bytes: 400 };
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: vi.fn().mockResolvedValue(payload)
  });
  vi.stubGlobal("fetch", fetchMock);

  await expect(getStorageSummary("secret")).resolves.toEqual(payload);
  expect(fetchMock).toHaveBeenCalledWith("/api/storage", {
    headers: {
      "Content-Type": "application/json",
      Authorization: "Bearer secret"
    }
  });
  vi.unstubAllGlobals();
});


test("closes a report event stream normally when its terminal snapshot arrives", () => {
  class FakeEventSource {
    listeners = new Map<string, (event: MessageEvent) => void>();
    close = vi.fn();
    addEventListener(name: string, callback: EventListenerOrEventListenerObject) {
      this.listeners.set(name, callback as (event: MessageEvent) => void);
    }
  }
  vi.stubGlobal("EventSource", FakeEventSource);
  const onJob = vi.fn();
  const source = openJobEvents("secret", "job-1", onJob) as unknown as FakeEventSource;
  source.listeners.get("progress")?.({
    data: JSON.stringify({ id: "job-1", status: "completed" })
  } as MessageEvent);

  expect(onJob).toHaveBeenCalledWith({ id: "job-1", status: "completed" });
  expect(source.close).toHaveBeenCalledOnce();
  vi.unstubAllGlobals();
});


test("closes a Browse event stream for cancelled terminal scans", () => {
  class FakeEventSource {
    listeners = new Map<string, (event: MessageEvent) => void>();
    close = vi.fn();
    addEventListener(name: string, callback: EventListenerOrEventListenerObject) {
      this.listeners.set(name, callback as (event: MessageEvent) => void);
    }
  }
  vi.stubGlobal("EventSource", FakeEventSource);
  const onScan = vi.fn();
  const source = openBrowseEvents("secret", "browse-1", onScan) as unknown as FakeEventSource;
  source.listeners.get("progress")?.({
    data: JSON.stringify({ id: "browse-1", status: "cancelled" })
  } as MessageEvent);

  expect(onScan).toHaveBeenCalledWith({ id: "browse-1", status: "cancelled" });
  expect(source.close).toHaveBeenCalledOnce();
  vi.unstubAllGlobals();
});
