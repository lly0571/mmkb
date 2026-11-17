// Shared timing and paging defaults. Kept out of component modules so fast refresh only sees components.

/** Status polling for one resource while the tab is visible. */
export const POLL_INTERVAL_MS = 2000
export const POLL_MAX_INTERVAL_MS = 30_000
/** After this window we stop implying progress and tell the user to check back later. */
export const POLL_OBSERVE_WINDOW_MS = 120_000

/** One shared interval for the visible browse page; cards never poll individually. */
export const LIBRARY_AUTO_REFRESH_MS = 5000
export const LIBRARY_PAGE_SIZES = [10, 20, 50, 100]
