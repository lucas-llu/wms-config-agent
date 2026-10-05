export function cleanEvidence(text: string): string {
  return text
    .replace(/\[IMAGE\s*:[^\]\r\n]*(?:\]|$)/gi, "")
    .replace(/\b[a-f0-9]{8,64}_\d+_\d+\]?/gi, "")
    .replace(/\s+/g, " ")
    .trim();
}
export function sidebarWidth(value: number): number {
  return Math.min(420, Math.max(240, Number.isFinite(value) ? value : 288));
}
export function canSend(message: string, busy: boolean): boolean {
  return !busy && message.trim().length > 0 && message.length <= 16000;
}
export function canReview(
  role: string | undefined,
  status: string,
  historical: boolean,
): boolean {
  return (
    !historical &&
    status === "review_required" &&
    ["reviewer", "workspace_admin"].includes(role || "")
  );
}
