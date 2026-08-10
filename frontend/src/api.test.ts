import { clearToken, initialToken, openJobEvents } from "./api";


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
