// Build-time constants for the Zero Zone build. Written as bare
// `import.meta.env.X` expressions so Vite substitutes them statically and the
// upstream build drops every Zero Zone branch (dead-code elimination).
import { normalizeSourceUrl } from "./mode";

export const IS_ZZ: boolean = import.meta.env.MODE === "zerozone";

/** null when VITE_ZZ_SOURCE_URL is not set at build time. */
export const ZZ_SOURCE_URL: string | null = IS_ZZ
  ? normalizeSourceUrl(import.meta.env.VITE_ZZ_SOURCE_URL)
  : null;
