// Admin-live alert strip state: pure helpers (no DOM) so they run under the
// node test runner. Payloads come from the SSE `alert` event (already pseudonymised
// on the ZZ side).

export const ALERT_MAX = 3;
export const ALERT_TTL_MS = 15_000;

export interface AlertItem {
  id: number;
  rule: string;
  severity: "warn" | "high";
  text: string;
  attacker?: string;
  victim?: string;
  expiresAt: number;
}

const str = (v: unknown, max: number): string | undefined =>
  typeof v === "string" && v.trim() ? v.slice(0, max) : undefined;
const nm = (v: unknown): string | undefined =>
  v && typeof v === "object" ? str((v as { name?: unknown }).name, 64) : undefined;

/** Validate an unknown SSE payload; null when it is not a usable alert. */
export function parseAlert(payload: unknown, now: number, id: number): AlertItem | null {
  if (!payload || typeof payload !== "object") return null;
  const o = payload as Record<string, unknown>;
  const rule = str(o.rule, 64);
  const text = str(o.text, 300);
  if (!rule || !text) return null;
  return {
    id, rule, text,
    severity: o.severity === "high" ? "high" : "warn",
    attacker: nm(o.attacker), victim: nm(o.victim),
    expiresAt: now + ALERT_TTL_MS,
  };
}

/** Newest first; at most ALERT_MAX kept. */
export function pushAlert(list: readonly AlertItem[], item: AlertItem): AlertItem[] {
  return [item, ...list].slice(0, ALERT_MAX);
}

export function expireAlerts(list: readonly AlertItem[], now: number): AlertItem[] {
  const out = list.filter((a) => a.expiresAt > now);
  return out.length === list.length ? (list as AlertItem[]) : out;
}

export function dismissAlert(list: readonly AlertItem[], id: number): AlertItem[] {
  return list.filter((a) => a.id !== id);
}

/** Translation key for the rule badge. */
export function ruleKey(rule: string): string {
  return rule === "teamkill" ? "alert.rule.teamkill" : "alert.rule.default";
}
