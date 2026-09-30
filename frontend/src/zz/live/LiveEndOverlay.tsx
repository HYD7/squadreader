// "Maç bitti" layer of the live viewer. Presentational: the state machine
// (liveEnd.ts) decides what to show, this only draws it in Turkish and calls
// back. It reuses the viewer's existing overlay card styles.
import type { EndState } from "./liveEnd";
import { LIVE_TR, endReadyText } from "./liveText";

export function LiveEndOverlay(props: {
  state: EndState;
  /** Administrator view: information only. */
  admin: boolean;
  onNext: () => void;
  onGoto: () => void;
  onClose: () => void;
}) {
  const { state, admin } = props;
  if (state.phase === "hidden") return null;

  let sub: string;
  let sub2: string | null = null;
  let spinner = false;
  if (state.phase === "pending") {
    sub = LIVE_TR.endPending;
    sub2 = LIVE_TR.endPendingSub;
    spinner = true;
  } else if (state.phase === "ready") {
    sub = state.countdown !== null ? endReadyText(state.countdown)
                                   : LIVE_TR.endNoToken;
  } else if (state.phase === "failed") {
    sub = LIVE_TR.endFailed;
    sub2 = LIVE_TR.endFailedSub;
  } else {
    sub = admin ? LIVE_TR.endAdminSub : LIVE_TR.endNoToken;
  }

  return (
    <div className="buf-overlay zz-live-end" role="status" aria-live="polite">
      <div className="buf-card">
        {spinner && <div className="buf-ring" />}
        <div className="buf-title">{LIVE_TR.matchEnded}</div>
        <div className="buf-sub">{sub}</div>
        {sub2 && <div className="buf-sub">{sub2}</div>}
        <div className="buf-actions zz-live-actions">
          {admin ? (
            <button className="buf-ghost" onClick={props.onClose}>{LIVE_TR.close}</button>
          ) : (
            <>
              <button className="buf-ok" onClick={props.onNext}>{LIVE_TR.nextMatch}</button>
              {state.phase === "ready" && (
                <button className="buf-ghost" onClick={props.onGoto}>{LIVE_TR.gotoMatch}</button>
              )}
              {(state.phase === "failed" || state.phase === "info") && (
                <a className="buf-ghost zz-live-link" href="/maclar" target="_top">{LIVE_TR.matchesList}</a>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
