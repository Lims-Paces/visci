"use client";

/**
 * The TanStack Query cache, made available to the whole app.
 *
 * WHAT A QueryClient IS
 * ---------------------
 * One in-memory cache of server data, keyed by "query keys" you choose. When
 * two components both ask for ["job", "abc"], they do NOT make two requests —
 * they read the same cache entry, and one fetch serves both.
 *
 * That single fact fixes the bug this replaces: the sidebar and the job page
 * were polling separately and disagreeing for up to 8 seconds about whether a
 * film had finished.
 *
 * WHY useState AROUND THE CLIENT
 * ------------------------------
 * `useState(() => new QueryClient())` creates it ONCE per component instance.
 * Writing `new QueryClient()` directly in the render body would build a fresh
 * empty cache on every render, throwing away everything cached so far.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

export default function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // How long data is considered fresh. Within this window, a
            // component mounting reads the cache and makes NO request.
            //
            // 10s suits us: job status changes as shots land, so we want it
            // reasonably current, but re-reading it on every navigation would
            // be wasteful.
            staleTime: 10_000,

            // Keep unused data around for 5 minutes after the last component
            // stops using it. This is what makes navigating back to a film
            // instant — the data is still cached, so it renders immediately
            // while a background refetch checks for changes.
            gcTime: 5 * 60_000,

            // Retry failed requests with exponential backoff. This is the fix
            // for the old hand-rolled poller, where a single network blip
            // during a 15-minute generation killed the poll loop permanently
            // and the page silently stopped updating.
            retry: 3,
            retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 30_000),

            // Refetch when the user comes back to the tab. Someone who
            // switches away for five minutes should see current state on
            // return, not a stale snapshot.
            refetchOnWindowFocus: true,
          },
        },
      })
  );

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
