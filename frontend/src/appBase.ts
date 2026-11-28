/**
 * The SPA is mounted at /app — see <BrowserRouter basename="/app"> in main.tsx.
 *
 * Any path that is written by hand (history.replaceState, window.location, a raw href)
 * must include this prefix. A bare '/search?q=…' sits OUTSIDE the router's basename, so the
 * router matches nothing and the page renders blank — which is what happened when the URL
 * was rewritten on search and the user then pressed back from a ruling page.
 */
export const APP_BASE = '/app'
