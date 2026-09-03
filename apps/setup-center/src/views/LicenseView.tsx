// ─── LicenseView: offline license activation ───
//
// Shown after login when the backend reports the install is unlicensed,
// expired, or bound to a different machine (HTTP 402 from the license gate).
//
// Gate order is setup → login → license, and that ordering is deliberate:
// activation is admin-only, so the user must already be authenticated when
// they land here. See ``middleware_license_gate.py``.
//
// Layout mirrors SetupView so the whole first-run path feels like one flow.

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import logoUrl from "../assets/logo-new.png";
import { IS_CAPACITOR } from "../platform/detect";
import {
  activateLicense,
  fetchFingerprint,
  type FingerprintInfo,
  type LicenseState,
  type LicenseStatus,
} from "../platform/license";

export function LicenseView({
  apiBaseUrl,
  state,
  detail,
  onActivated,
}: {
  apiBaseUrl: string;
  state?: LicenseState;
  detail?: string;
  onActivated: () => void;
}) {
  const { t } = useTranslation();
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [fingerprint, setFingerprint] = useState<FingerprintInfo | null>(null);
  const [fpLoading, setFpLoading] = useState(true);
  const [copied, setCopied] = useState(false);
  const [done, setDone] = useState(false);
  const [granted, setGranted] = useState<LicenseStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const info = await fetchFingerprint(apiBaseUrl);
      if (!cancelled) {
        setFingerprint(info);
        setFpLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [apiBaseUrl]);

  const handleCopy = useCallback(async () => {
    if (!fingerprint) return;
    try {
      await navigator.clipboard.writeText(fingerprint.fingerprint);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API needs a secure context; the value is selectable anyway.
    }
  }, [fingerprint]);

  const handleSubmit = useCallback(
    async (e?: React.FormEvent) => {
      e?.preventDefault();
      setError(null);
      const trimmed = code.trim();
      if (!trimmed) {
        setError(t("license.errorEmpty"));
        return;
      }

      setLoading(true);
      const result = await activateLicense(trimmed, apiBaseUrl);
      setLoading(false);

      if (!result.success) {
        setError(result.error || t("license.errorGeneric"));
        return;
      }
      // Plugins / skills / IM channels are wired up at process start, so a
      // freshly-widened feature set only takes effect after a restart. The
      // API surface itself unblocks immediately.
      setGranted(result.license ?? null);
      setDone(true);
    },
    [apiBaseUrl, code, t],
  );

  const reasonKey =
    state === "expired"
      ? "license.reasonExpired"
      : state === "mismatch"
        ? "license.reasonMismatch"
        : state === "invalid"
          ? "license.reasonInvalid"
          : "license.reasonMissing";

  return (
    <div style={pageStyle}>
      <form onSubmit={handleSubmit} style={cardStyle}>
        <img src={logoUrl} alt="Mclaw" style={logoStyle} />
        <h2 style={titleStyle}>{t("license.title")}</h2>

        {done ? (
          <div style={successStyle}>
            <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 6 }}>
              {t("license.successTitle")}
            </div>
            {/* Echo what was actually granted: the customer should be able to
                confirm the serial and expiry match what they were sold before
                leaving this screen. */}
            {granted && (
              <dl style={grantGridStyle}>
                {granted.customer && (
                  <>
                    <dt style={grantKeyStyle}>{t("license.fieldCustomer")}</dt>
                    <dd style={grantValStyle}>{granted.customer}</dd>
                  </>
                )}
                {granted.serial && (
                  <>
                    <dt style={grantKeyStyle}>{t("license.fieldSerial")}</dt>
                    <dd style={grantValStyle}>{granted.serial}</dd>
                  </>
                )}
                {granted.expires && (
                  <>
                    <dt style={grantKeyStyle}>{t("license.fieldExpires")}</dt>
                    <dd style={grantValStyle}>
                      {granted.expires}
                      {granted.days_remaining > 0 &&
                        ` (${t("license.daysRemaining", {
                          days: granted.days_remaining,
                        })})`}
                    </dd>
                  </>
                )}
                <>
                  <dt style={grantKeyStyle}>{t("license.fieldMaxUsers")}</dt>
                  <dd style={grantValStyle}>
                    {!granted.max_users || granted.max_users <= 0
                      ? t("license.usersUnlimited")
                      : granted.max_users}
                  </dd>
                </>
                {granted.tier && (
                  <>
                    <dt style={grantKeyStyle}>{t("license.fieldTier")}</dt>
                    <dd style={grantValStyle}>{granted.tier}</dd>
                  </>
                )}
              </dl>
            )}
            <div style={{ fontSize: 13, lineHeight: 1.6 }}>
              {t("license.successRestart")}
            </div>
            <button type="button" onClick={onActivated} style={continueBtnStyle}>
              {t("license.continue")}
            </button>
          </div>
        ) : (
          <>
            <p style={introStyle}>{t(reasonKey)}</p>
            {detail && <div style={detailStyle}>{detail}</div>}

            {/* Machine fingerprint — the customer sends this to the vendor. */}
            <div style={fpBoxStyle}>
              <div style={fpLabelStyle}>{t("license.fingerprintLabel")}</div>
              {fpLoading ? (
                <div style={{ fontSize: 13, color: "var(--text3, #94a3b8)" }}>
                  {t("license.fingerprintLoading")}
                </div>
              ) : fingerprint ? (
                <>
                  <div style={fpValueRowStyle}>
                    <code style={fpCodeStyle}>{fingerprint.fingerprint}</code>
                    <button type="button" onClick={handleCopy} style={copyBtnStyle}>
                      {copied ? t("license.copied") : t("license.copy")}
                    </button>
                  </div>
                  {!fingerprint.sufficient && (
                    <div style={fpWarnStyle}>
                      {t("license.fingerprintInsufficient", {
                        usable: fingerprint.usable_segments,
                        required: fingerprint.min_required,
                      })}
                    </div>
                  )}
                </>
              ) : (
                <div style={fpWarnStyle}>{t("license.fingerprintFailed")}</div>
              )}
            </div>

            <p style={hintStyle}>{t("license.sendHint")}</p>

            {error && <div style={errorStyle}>{error}</div>}

            <label style={labelStyle}>{t("license.codeLabel")}</label>
            <textarea
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder={t("license.codePlaceholder")}
              disabled={loading}
              rows={4}
              style={textareaStyle}
              onFocus={(e) => {
                e.target.style.borderColor = "var(--primary, #2563eb)";
              }}
              onBlur={(e) => {
                e.target.style.borderColor = "var(--line, #e2e8f0)";
              }}
            />

            <button
              type="submit"
              disabled={loading || code.trim().length === 0}
              style={submitStyle(loading || code.trim().length === 0)}
            >
              {loading ? t("license.submitting") : t("license.submit")}
            </button>
          </>
        )}
      </form>
    </div>
  );
}

// ── styles ──

const pageStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  minHeight: "100vh",
  width: "100vw",
  background:
    "linear-gradient(135deg, var(--bg, #f8fafc) 0%, var(--panel, #e2e8f0) 100%)",
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
  color: "var(--text, #334155)",
  padding: 32,
  paddingTop: IS_CAPACITOR ? "max(32px, env(safe-area-inset-top))" : 32,
  boxSizing: "border-box",
};

const cardStyle: React.CSSProperties = {
  background: "var(--panel2, #fff)",
  borderRadius: 16,
  boxShadow: "0 4px 24px rgba(0,0,0,0.08)",
  padding: "40px 48px",
  maxWidth: 520,
  width: "100%",
  textAlign: "center",
};

const logoStyle: React.CSSProperties = {
  width: 56,
  height: 56,
  marginBottom: 12,
  borderRadius: 12,
};

const titleStyle: React.CSSProperties = {
  margin: "0 0 8px",
  fontSize: 20,
  fontWeight: 600,
  color: "var(--text, #1e293b)",
};

const introStyle: React.CSSProperties = {
  margin: "0 0 12px",
  fontSize: 13,
  color: "var(--text3, #64748b)",
  lineHeight: 1.6,
  textAlign: "left",
};

const detailStyle: React.CSSProperties = {
  background: "var(--warn-bg, #fffbeb)",
  color: "var(--warn, #b45309)",
  borderRadius: 8,
  padding: "8px 12px",
  fontSize: 12,
  marginBottom: 16,
  textAlign: "left",
  lineHeight: 1.6,
};

const fpBoxStyle: React.CSSProperties = {
  background: "var(--bg, #f8fafc)",
  border: "1px solid var(--line, #e2e8f0)",
  borderRadius: 10,
  padding: "12px 14px",
  marginBottom: 12,
  textAlign: "left",
};

const fpLabelStyle: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 600,
  color: "var(--text2, #475569)",
  marginBottom: 8,
};

const fpValueRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
};

const fpCodeStyle: React.CSSProperties = {
  flex: 1,
  fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace",
  fontSize: 14,
  letterSpacing: "0.05em",
  color: "var(--text, #1e293b)",
  userSelect: "all",
  wordBreak: "break-all",
};

const copyBtnStyle: React.CSSProperties = {
  background: "transparent",
  border: "1px solid var(--line, #cbd5e1)",
  borderRadius: 6,
  padding: "4px 10px",
  fontSize: 12,
  color: "var(--text2, #475569)",
  cursor: "pointer",
  whiteSpace: "nowrap",
};

const fpWarnStyle: React.CSSProperties = {
  marginTop: 8,
  fontSize: 12,
  color: "var(--error, #dc2626)",
  lineHeight: 1.6,
};

const hintStyle: React.CSSProperties = {
  margin: "0 0 16px",
  fontSize: 12,
  color: "var(--text3, #94a3b8)",
  lineHeight: 1.6,
  textAlign: "left",
};

const errorStyle: React.CSSProperties = {
  background: "var(--error-bg, #fef2f2)",
  color: "var(--error, #dc2626)",
  borderRadius: 8,
  padding: "8px 12px",
  fontSize: 13,
  marginBottom: 16,
  textAlign: "left",
  whiteSpace: "pre-line",
  lineHeight: 1.6,
};

const successStyle: React.CSSProperties = {
  background: "var(--success-bg, #f0fdf4)",
  color: "var(--success, #15803d)",
  borderRadius: 8,
  padding: "16px 14px",
  marginTop: 12,
  textAlign: "left",
};

const grantGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "auto 1fr",
  gap: "6px 12px",
  margin: "12px 0",
  padding: "10px 12px",
  background: "var(--card, rgba(255,255,255,0.6))",
  border: "1px solid var(--success-line, #bbf7d0)",
  borderRadius: 8,
};

const grantKeyStyle: React.CSSProperties = {
  fontSize: 12,
  color: "var(--text2, #475569)",
  whiteSpace: "nowrap",
};

const grantValStyle: React.CSSProperties = {
  fontSize: 12.5,
  fontWeight: 600,
  color: "var(--text, #1e293b)",
  margin: 0,
  wordBreak: "break-word",
};

const continueBtnStyle: React.CSSProperties = {
  marginTop: 14,
  width: "100%",
  padding: "9px 14px",
  fontSize: 13,
  fontWeight: 600,
  color: "#fff",
  background: "var(--success, #15803d)",
  border: "none",
  borderRadius: 8,
  cursor: "pointer",
};

const labelStyle: React.CSSProperties = {
  display: "block",
  textAlign: "left",
  fontSize: 13,
  fontWeight: 500,
  marginBottom: 6,
  color: "var(--text2, #475569)",
};

const textareaStyle: React.CSSProperties = {
  width: "100%",
  padding: "10px 14px",
  fontSize: 13,
  fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace",
  borderRadius: 10,
  border: "1px solid var(--line, #e2e8f0)",
  background: "var(--bg, #f8fafc)",
  color: "var(--text, #1e293b)",
  outline: "none",
  boxSizing: "border-box",
  resize: "vertical",
  transition: "border-color 0.15s",
  wordBreak: "break-all",
};

function submitStyle(disabled: boolean): React.CSSProperties {
  return {
    width: "100%",
    marginTop: 20,
    background: disabled
      ? "var(--text3, #94a3b8)"
      : "linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%)",
    color: "#fff",
    border: "none",
    borderRadius: 10,
    padding: "10px 0",
    fontSize: 15,
    fontWeight: 600,
    cursor: disabled ? "not-allowed" : "pointer",
    boxShadow: "0 2px 8px rgba(37,99,235,0.3)",
    transition: "transform 0.1s, opacity 0.15s",
    opacity: disabled ? 0.7 : 1,
  };
}
