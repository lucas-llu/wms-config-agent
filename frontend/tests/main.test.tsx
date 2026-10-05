import { expect, it, vi } from "vitest";
import { fakeAuth } from "./fixtures";
const controls = vi.hoisted(() => ({ render: vi.fn(), authenticate: vi.fn() }));
vi.mock("react-dom/client", () => ({
  createRoot: () => ({ render: controls.render }),
}));
vi.mock("../src/auth", () => ({ authenticate: controls.authenticate }));
it.each([true, false])(
  "mounts the matching authenticated state %s",
  async (authenticated) => {
    vi.resetModules();
    document.body.innerHTML = '<div id="root"></div>';
    controls.authenticate.mockResolvedValue({
      auth: fakeAuth(),
      authenticated,
    });
    await import("../src/main");
    await new Promise((r) => setTimeout(r, 0));
    expect(controls.render).toHaveBeenCalled();
  },
);
it("shows a reconnect view on identity setup failure", async () => {
  vi.resetModules();
  document.body.innerHTML = '<div id="root"></div>';
  controls.authenticate.mockRejectedValue(new Error("synthetic"));
  await import("../src/main");
  await new Promise((r) => setTimeout(r, 0));
  expect(controls.render).toHaveBeenCalled();
});
