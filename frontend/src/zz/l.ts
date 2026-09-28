// Component-side helpers for the Zero Zone Turkish text (see tr.ts).
//
// `L(key, en)` returns the Turkish string in the Zero Zone build and the given
// English string otherwise, so the upstream build's text and behaviour are
// unchanged. IS_ZZ is a build-time constant: in the upstream build the Turkish
// branch is dead code and the dictionary is dropped from the bundle.
import { IS_ZZ } from "./build";
import { t, tEn, type TrKey, type TrParams } from "./tr";

/** Turkish text in the Zero Zone build, `en` (already formatted) otherwise. */
export function L(key: TrKey, en: string, params?: TrParams): string {
  return IS_ZZ ? t(key, params) : en;
}

/** Zero Zone build: translate an English string emitted by a shared helper. */
export function LEn(en: string): string {
  return IS_ZZ ? tEn(en) : en;
}
