// Admin-live alert strip: up to 3 stacked alerts (red = high, orange = warn),
// each fades after 15 s or on click. Only rendered in admin-live mode.
import { useEffect, useState } from "react";
import { t, type TrKey } from "../tr";
import {
  dismissAlert, expireAlerts, ruleKey, type AlertItem,
} from "./alertState";

export function LiveAlertStrip(props: { items: AlertItem[];
                                         onChange(next: AlertItem[]): void }) {
  const { items, onChange } = props;
  const [, tick] = useState(0);
  useEffect(() => {
    if (items.length === 0) return;
    const h = window.setInterval(() => {
      const next = expireAlerts(items, Date.now());
      if (next !== items) onChange(next);
      else tick((n) => n + 1);
    }, 1000);
    return () => window.clearInterval(h);
  }, [items, onChange]);
  if (items.length === 0) return null;
  return (
    <div className="zz-alert-strip" role="alert">
      {items.map((a) => (
        <button key={a.id} type="button" className={`zz-alert zz-alert-${a.severity}`}
                title={t("alert.dismiss")}
                onClick={() => onChange(dismissAlert(items, a.id))}>
          <span className="zz-alert-rule">{t(ruleKey(a.rule) as TrKey)}</span>
          <span className="zz-alert-text">{a.text}</span>
        </button>
      ))}
    </div>
  );
}
