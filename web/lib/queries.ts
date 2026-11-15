"use client";

/**
 * Every server-data hook in the app.
 *
 * WHY QUERY KEYS LIVE IN ONE PLACE
 * --------------------------------
 * A query key is an array identifying a cache entry: ["job", "abc123"]. Two
 * components using the SAME key share one cache entry and one fetch; two
 * components using different keys do not.
 *
 * So a typo — ["jobs"] in one file, ["job"] in another — silently creates two
 * caches that never sync, and the UI mysteriously fails to update. Defining
 * them once removes that entire failure mode.
 *
 * The array shape allows hierarchy: invalidating ["job"] clears every job,
 * while ["job", "abc"] clears only that one.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createJob,
  getConfig,
  getJob,
  listJobs,
  resumeJob,
  type Format,
  type JobStatus,
  type Mode,
} from "./api";

export const keys = {
  jobs: ["jobs"] as const,
  job: (id: string) => ["job", id] as const,
  config: ["config"] as const,
};

/**
 * One job's live status.
 *
 * THE POLLING PART
 * ----------------
 * `refetchInterval` accepts a FUNCTION, which is what makes this better than
 * the setTimeout loop it replaces: the interval is recomputed after every
 * fetch, so polling stops automatically the moment the job finishes. No
 * cleanup, no risk of a loop outliving the component.
 */
export function useJob(id: string) {
  return useQuery({
    queryKey: keys.job(id),
    queryFn: () => getJob(id),

    refetchInterval: (query) => {
      const job = query.state.data as JobStatus | undefined;
      // Finished or failed: stop polling entirely.
      if (!job || job.finished || job.error) return false;
      return 2500;
    },

    // Do NOT poll a backgrounded tab. A user who leaves this open on their
    // phone should not be making a request every 2.5 seconds for an hour.
    refetchIntervalInBackground: false,
  });
}

/**
 * Every film, for the sidebar.
 *
 * Polls more slowly than the job page — this list only needs to notice new
 * films and finished ones, not track shot-by-shot progress.
 */
export function useJobs() {
  return useQuery({
    queryKey: keys.jobs,
    queryFn: listJobs,
    refetchInterval: 8000,
    refetchIntervalInBackground: false,
  });
}

/**
 * Server capabilities and cost estimates.
 *
 * `staleTime: Infinity` because this never changes while the app is open.
 * Fetched once, then read from cache forever — no repeated requests.
 */
export function useConfig() {
  return useQuery({
    queryKey: keys.config,
    queryFn: getConfig,
    staleTime: Infinity,
  });
}

/**
 * Start a new film.
 *
 * A MUTATION, NOT A QUERY
 * -----------------------
 * Queries READ data and are cached, deduplicated, and retried. Mutations WRITE
 * data — they run once when you call them, are never cached, and never retried
 * automatically. Retrying a "create film" on a network blip could produce two
 * films and charge twice.
 *
 * `onSuccess` seeds the cache with the new job and marks the film list stale,
 * so the sidebar shows the new film immediately rather than up to 8 seconds
 * later on its next poll.
 */
/**
 * Resume an interrupted job.
 *
 * A mutation, not a query: it writes. On success it seeds the job cache so the
 * page immediately shows the run as live again rather than waiting for the
 * next poll to clear the error.
 */
export function useResumeJob(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (draft: boolean) => resumeJob(id, draft),
    onSuccess: (job) => {
      qc.setQueryData(keys.job(id), job);
      qc.invalidateQueries({ queryKey: keys.jobs });
    },
  });
}


export function useCreateJob() {
  const qc = useQueryClient();

  return useMutation({
    mutationFn: (input: {
      premise: string;
      mode: Mode;
      format: Format;
      real: boolean;
      draft: boolean;
    }) => createJob(input),

    onSuccess: (job) => {
      // Put the response straight into the cache under its own key, so the job
      // page renders instantly on navigation instead of showing "Loading…"
      // while it fetches data we already hold.
      qc.setQueryData(keys.job(job.job_id), job);

      // Mark the film list stale so the sidebar refetches now.
      qc.invalidateQueries({ queryKey: keys.jobs });
    },
  });
}
