// Navigation out of the live view. The viewer normally runs inside the
// portal's iframe; a redirect to the match page must replace the TOP window,
// not only the frame. Same origin, but be safe anyway.

export interface NavWindow {
  location: { assign(url: string): void };
  top?: NavWindow | null;
}

export function navigateTop(url: string, win: NavWindow): void {
  try {
    (win.top ?? win).location.assign(url);
  } catch {
    win.location.assign(url);
  }
}
