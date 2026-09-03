// ─── Offline license API client ───
//
// All three endpoints require an authenticated caller (they sit behind the
// auth middleware), so every call goes through ``authFetch``. ``activate``
// additionally requires an admin token — the backend enforces that, we just
// surface the 403.
//
// These are separate from ``auth.ts`` because the license flow runs *after*
// login, not as part of it.

import { authFetch } from "./auth";

export type LicenseState =
  | "active"
  | "grace"
  | "missing"
  | "invalid"
  | "mismatch"
  | "expired"
  | "disabled";

export interface LicenseStatus {
  state: LicenseState;
  allows_access: boolean;
  should_warn: boolean;
  message: string;
  days_remaining: number;
  grace_period_days?: number;
  serial?: string;
  customer?: string;
  issued?: string;
  expires?: string;
  tier?: string;
  max_users?: number;
  features?: string[];
}

export interface FingerprintComponent {
  key: string;
  label: string;
  available: boolean;
}

export interface FingerprintInfo {
  fingerprint: string;
  usable_segments: number;
  total_segments: number;
  min_required: number;
  sufficient: boolean;
  components: FingerprintComponent[];
}

/**
 * Fetch the current license state.
 *
 * Returns ``null`` when the request itself fails (backend down, network
 * error). Callers must treat ``null`` as "unknown" and NOT as "unlicensed" —
 * showing the activation screen because a health probe timed out would lock
 * a paying customer out of their own install.
 */
export async function fetchLicenseStatus(
  apiBase = "",
): Promise<LicenseStatus | null> {
  try {
    const res = await authFetch(
      `${apiBase}/api/license/status`,
      { signal: AbortSignal.timeout(10_000) },
      apiBase,
    );
    if (!res.ok) return null;
    return (await res.json()) as LicenseStatus;
  } catch {
    return null;
  }
}

/** Read this machine's hardware fingerprint (to send to the vendor). */
export async function fetchFingerprint(
  apiBase = "",
): Promise<FingerprintInfo | null> {
  try {
    const res = await authFetch(
      `${apiBase}/api/license/fingerprint`,
      // Collection shells out to PowerShell (~1.3s cold); the backend caches
      // it per-process, but the first call after a restart pays full price.
      { signal: AbortSignal.timeout(40_000) },
      apiBase,
    );
    if (!res.ok) return null;
    return (await res.json()) as FingerprintInfo;
  } catch {
    return null;
  }
}

/**
 * Submit a license code. Admin only.
 *
 * On failure the backend does not persist anything, so a bad paste can never
 * clobber a working license.
 */
export async function activateLicense(
  code: string,
  apiBase = "",
): Promise<{
  success: boolean;
  error?: string;
  restartRequired?: boolean;
  license?: LicenseStatus;
}> {
  try {
    const res = await authFetch(
      `${apiBase}/api/license/activate`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
        signal: AbortSignal.timeout(40_000),
      },
      apiBase,
    );
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      return { success: false, error: data.detail || `HTTP ${res.status}` };
    }
    const data = await res.json();
    return {
      success: true,
      restartRequired: data.restart_required === true,
      license: data.license as LicenseStatus | undefined,
    };
  } catch (e) {
    return { success: false, error: String(e) };
  }
}
