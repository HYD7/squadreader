// Zero Zone entry (index.zerozone.html). Replay + live viewer; see mode.ts.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import ZzApp from "./ZzApp";
import { installZzLiveHook, installZzReplayHook } from "./testHook";
import "../style.css";
import "../theme-apple.css";
import "./zz.css";

// The dark viewer theme that matches the (dark) map.
document.documentElement.setAttribute("data-theme", "apple");
installZzReplayHook();
installZzLiveHook();

const root = document.getElementById("root");
if (!root) throw new Error("missing #root element");
createRoot(root).render(<StrictMode><ZzApp /></StrictMode>);
