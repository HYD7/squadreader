// Small "Kaynak kodu" link (AGPL source access). Renders nothing unless
// VITE_ZZ_SOURCE_URL was set at build time.
import { ZZ_SOURCE_URL } from "./build";
import { TR } from "./mode";

export function SourceLink() {
  if (!ZZ_SOURCE_URL) return null;
  return (
    <a className="zz-source" href={ZZ_SOURCE_URL}
       target="_blank" rel="noopener noreferrer">{TR.source}</a>
  );
}
