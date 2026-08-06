import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link2, RefreshCw, Eraser } from "lucide-react";
import { Button } from "@/components/ui/button";
import { safeFetch } from "../providers";
import { notifyError, notifySuccess } from "../utils/notify";

export type LinkDiagnostic = {
  requested_url?: string;
  final_url?: string;
  redirect_chain?: string[];
  status_code?: number;
  content_type?: string;
  status?: "ok" | "error" | string;
  error_code?: string;
  hostname?: string;
};

type ClearResponse = { ok: boolean; cleared?: Record<string, boolean> };

export interface LinkDiagnosticsPanelProps {
  httpApiBase: () => string;
  initialDiagnostic?: LinkDiagnostic | null;
}

function shortUrl(url: string | undefined, max = 80): string {
  if (!url) return "";
  if (url.length <= max) return url;
  return url.slice(0, max - 1) + "…";
}

export function LinkDiagnosticsPanel({ httpApiBase, initialDiagnostic }: LinkDiagnosticsPanelProps) {
  const { t } = useTranslation();
  const [diag, setDiag] = useState<LinkDiagnostic | null>(initialDiagnostic ?? null);
  const [loading, setLoading] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [statusText, setStatusText] = useState("");

  const applyDiagnostic = useCallback((
    body: LinkDiagnostic | Record<string, never> | null | undefined,
    showResult: boolean,
  ) => {
    if (body && Object.keys(body).length > 0) {
      setDiag(body as LinkDiagnostic);
      if (showResult) {
        setStatusText(t("status.linkDiag.refreshed"));
      }
    } else {
      setDiag(null);
      if (showResult) {
        setStatusText(t("status.linkDiag.refreshedEmpty"));
      }
    }
  }, [t]);

  const refresh = async (showResult = true) => {
    setLoading(true);
    try {
      const resp = await safeFetch(`${httpApiBase()}/api/health`, {
        signal: AbortSignal.timeout(5_000),
      });
      const body = await resp.json();
      applyDiagnostic(body?.last_link_diagnostic || null, showResult);
    } catch (e) {
      if (showResult) {
        setStatusText(t("status.linkDiag.refreshFailedDetail", {
          error: String(e),
        }));
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    applyDiagnostic(initialDiagnostic || null, false);
  }, [applyDiagnostic, initialDiagnostic]);

  const onClear = async () => {
    setClearing(true);
    try {
      const resp = await safeFetch(
        `${httpApiBase()}/api/diagnostics/clear-session-caches`,
        { method: "POST" },
      );
      if (resp.ok) {
        const body = (await resp.json()) as ClearResponse;
        const items = Object.entries(body.cleared || {})
          .filter(([, v]) => v)
          .map(([k]) => k);
        notifySuccess(
          t("status.linkDiag.cleared", {
            items: items.length > 0
              ? items.join(t("status.linkDiag.itemSeparator"))
              : "—",
          }),
        );
        setDiag(null);
        setStatusText(t("status.linkDiag.clearedHint"));
      } else {
        notifyError(`HTTP ${resp.status}`);
        setStatusText(t("status.linkDiag.clearFailed"));
      }
    } catch (e) {
      notifyError(String(e));
      setStatusText(t("status.linkDiag.clearFailedDetail", {
        error: String(e),
      }));
    } finally {
      setClearing(false);
    }
  };

  const requested = diag?.requested_url || "";
  const finalUrl = diag?.final_url || requested;
  const redirected = !!(requested && finalUrl && requested !== finalUrl);
  const isError = (diag?.status || "").toLowerCase() === "error";

  const errorReason = (() => {
    const code = (diag?.error_code || "").toString();
    switch (code) {
      case "binary_content":
        return t("status.linkDiag.reason.binary");
      case "domain_blocked":
        return t("status.linkDiag.reason.blocked");
      case "too_many_redirects":
        return t("status.linkDiag.reason.tooManyRedirects");
      case "network_error":
        return t("status.linkDiag.reason.network");
      case "empty_content":
        return t("status.linkDiag.reason.empty");
      case "redirect_missing_location":
        return t("status.linkDiag.reason.redirectInvalid");
      default:
        if (typeof diag?.status_code === "number" && diag.status_code >= 400) {
          return t("status.linkDiag.reason.httpError", {
            code: diag.status_code,
          });
        }
        return code || "";
    }
  })();

  return (
    <div className="statusPanelRow">
      <div className="statusPanelIcon">
        <Link2 size={18} />
      </div>
      <div className="statusPanelInfo">
        <div className="statusPanelTitle">
          {t("status.linkDiag.title")}
        </div>
        <div className="statusPanelDesc">
          {diag ? (
            isError ? (
              <span style={{ color: "var(--muted)" }}>
                {t("status.linkDiag.notRead", {
                  url: shortUrl(finalUrl || requested),
                })}
                {errorReason
                  ? t("status.linkDiag.reasonSuffix", {
                      reason: errorReason,
                    })
                  : ""}
              </span>
            ) : redirected ? (
              <span>
                {t("status.linkDiag.redirected", {
                  final: shortUrl(finalUrl),
                  requested: shortUrl(requested),
                })}
              </span>
            ) : (
              <span>
                {t("status.linkDiag.ok", {
                  final: shortUrl(finalUrl),
                })}
              </span>
            )
          ) : (
            <span style={{ opacity: 0.7 }}>
              {t("status.linkDiag.empty")}
            </span>
          )}
          {statusText && (
            <div style={{ marginTop: 4, fontSize: 12, opacity: 0.75 }}>
              {statusText}
            </div>
          )}
        </div>
      </div>
      <div className="statusPanelActions" style={{ display: "flex", gap: 6 }}>
        <Button
          size="sm"
          variant="outline"
          className="h-7 text-xs px-2.5"
          onClick={() => refresh(true)}
          disabled={loading || clearing}
          title={t("status.linkDiag.refresh") as string}
        >
          <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
          {loading ? t("status.checking") : t("status.linkDiag.refresh")}
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="h-7 text-xs px-2.5"
          onClick={onClear}
          disabled={clearing || loading}
          title={
            t("status.linkDiag.clearHint") as string
          }
        >
          {clearing ? <RefreshCw size={12} className="animate-spin" /> : <Eraser size={12} />}
          {clearing ? t("status.linkDiag.clearing") : t("status.linkDiag.clear")}
        </Button>
      </div>
    </div>
  );
}
