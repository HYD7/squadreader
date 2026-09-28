// Full-screen Turkish status card (invalid id / loading / error) that reuses
// the viewer's existing overlay styles.
import type { ReactNode } from "react";
import { SourceLink } from "./SourceLink";

export function ZzMessage(props: {
  title: string; sub?: string; spinner?: boolean; children?: ReactNode;
}) {
  return (
    <div className="zz-screen">
      <div className="buf-card" role="status">
        {props.spinner && <div className="buf-ring" />}
        <div className="buf-title">{props.title}</div>
        {props.sub && <div className="buf-sub">{props.sub}</div>}
        {props.children}
      </div>
      <SourceLink />
    </div>
  );
}
