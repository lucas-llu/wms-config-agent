import { expect, it } from "vitest";
import { canReview, canSend, cleanEvidence, sidebarWidth } from "../src/view";
it("cleans presentation markers without invented evidence", () => {
  expect(cleanEvidence("A [IMAGE: hash] 83bfe73792_13_2] B")).toBe("A B");
  expect(cleanEvidence("[IMAGE: unfinished")).toBe("");
});
it("bounds resize and respects IME-independent send constraints", () => {
  expect(sidebarWidth(200)).toBe(240);
  expect(sidebarWidth(500)).toBe(420);
  expect(sidebarWidth(NaN)).toBe(288);
  expect(sidebarWidth(300)).toBe(300);
  expect(canSend("hi", false)).toBe(true);
  expect(canSend("", false)).toBe(false);
  expect(canSend("hi", true)).toBe(false);
  expect(canSend("x".repeat(16001), false)).toBe(false);
});
it("requires a reviewer, latest revision and review-required state", () => {
  expect(canReview("reviewer", "review_required", false)).toBe(true);
  expect(canReview("workspace_admin", "review_required", false)).toBe(true);
  expect(canReview("member", "review_required", false)).toBe(false);
  expect(canReview(undefined, "review_required", false)).toBe(false);
  expect(canReview("reviewer", "paused", false)).toBe(false);
  expect(canReview("reviewer", "review_required", true)).toBe(false);
});
