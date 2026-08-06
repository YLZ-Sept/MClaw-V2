import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { AlertTriangle, CheckCircle2, RefreshCw, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { safeFetch } from "../providers";

type ConflictSource = {
  origin?: string;
  plugin_source?: string;
  path?: string;
};

type SkillConflict = {
  skill_id?: string;
  name?: string;
  action?: "rejected" | "overridden" | string;
  winner?: ConflictSource;
  shadowed?: ConflictSource;
};

export interface SkillConflictsPanelProps {
  httpApiBase: () => string;
}

function describeSource(t: TFunction, src?: ConflictSource): string {
  if (!src) return "—";
  const parts: string[] = [];
  if (src.origin) parts.push(describeOrigin(t, src.origin));
  if (src.plugin_source) parts.push(src.plugin_source);
  if (src.path) parts.push(src.path);
  return parts.join(" · ") || "—";
}

function describeOrigin(t: TFunction, origin?: string): string {
  switch (origin) {
    case "remote":
      return t("status.skillConflicts.origin.remote");
    case "project":
      return t("status.skillConflicts.origin.project");
    case "system":
      return t("status.skillConflicts.origin.system");
    case "marketplace":
      return t("status.skillConflicts.origin.marketplace");
    case "plugin":
      return t("status.skillConflicts.origin.plugin");
    default:
      return origin || t("status.skillConflicts.origin.unknown");
  }
}

export function SkillConflictsPanel({ httpApiBase }: SkillConflictsPanelProps) {
  const { t } = useTranslation();
  const [conflicts, setConflicts] = useState<SkillConflict[]>([]);
  const [loading, setLoading] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [statusText, setStatusText] = useState("");

  const refresh = async (showResult = true) => {
    setLoading(true);
    try {
      const resp = await safeFetch(`${httpApiBase()}/api/skills/conflicts`);
      if (resp.ok) {
        const body = (await resp.json()) as { conflicts?: SkillConflict[] };
        const next = Array.isArray(body.conflicts) ? body.conflicts : [];
        setConflicts(next);
        if (showResult) {
          setStatusText(t(next.length > 0
            ? "status.skillConflicts.refreshedNonEmpty"
            : "status.skillConflicts.refreshedEmpty"));
        }
      } else if (showResult) {
        setStatusText(t("status.skillConflicts.refreshFailed"));
      }
    } catch {
      if (showResult) setStatusText(t("status.skillConflicts.refreshOffline"));
    } finally {
      setLoading(false);
    }
  };

  const clearConflicts = async () => {
    setClearing(true);
    try {
      const resp = await safeFetch(`${httpApiBase()}/api/skills/conflicts/clear`, {
        method: "POST",
      });
      if (resp.ok) {
        setConflicts([]);
        setExpanded(false);
        setStatusText(t("status.skillConflicts.cleared"));
      } else {
        setStatusText(t("status.skillConflicts.clearFailed"));
      }
    } catch {
      setStatusText(t("status.skillConflicts.clearOffline"));
    } finally {
      setClearing(false);
    }
  };

  useEffect(() => {
    refresh(false);
    const onChange = () => {
      // Slight defer so the backend has a tick to update the registry.
      setTimeout(() => refresh(false), 200);
    };
    window.addEventListener("mclaw:skills-changed", onChange);
    const tabFocus = () => {
      if (!document.hidden) refresh();
    };
    document.addEventListener("visibilitychange", tabFocus);
    return () => {
      window.removeEventListener("mclaw:skills-changed", onChange);
      document.removeEventListener("visibilitychange", tabFocus);
    };
  }, []);

  const total = conflicts.length;

  return (
    <div className="statusPanelRow">
      <div className="statusPanelIcon">
        <AlertTriangle size={18} />
      </div>
      <div className="statusPanelInfo" style={{ minWidth: 0 }}>
        <div className="statusPanelTitle">
          {t("status.skillConflicts.title")}
        </div>
        <div className="statusPanelDesc">
          {total === 0 ? (
            <span style={{ opacity: 0.7 }}>
              {t("status.skillConflicts.empty")}
            </span>
          ) : (
            <span style={{ color: "#c0392b" }}>
              {t("status.skillConflicts.nonEmpty", {
                count: total,
              })}
            </span>
          )}
          {statusText && (
            <div style={{ marginTop: 4, fontSize: 12, opacity: 0.75 }}>
              {statusText}
            </div>
          )}
          {expanded && total > 0 && (
            <ul
              style={{
                marginTop: 6,
                paddingLeft: 16,
                fontSize: 12,
                opacity: 0.85,
                maxHeight: 180,
                overflow: "auto",
              }}
            >
              {conflicts.map((c, i) => {
                const action = c.action === "overridden"
                  ? t("status.skillConflicts.actionOverridden")
                  : t("status.skillConflicts.actionRejected");
                return (
                  <li key={i} style={{ marginBottom: 4 }}>
                    <strong>{c.skill_id || c.name || t("status.skillConflicts.unknownSkill")}</strong> · {action}
                    <div style={{ opacity: 0.75 }}>
                      {t("status.skillConflicts.currentSource", {
                        source: describeSource(t, c.winner),
                      })}
                    </div>
                    <div style={{ opacity: 0.6 }}>
                      {t("status.skillConflicts.shadowedSource", {
                        source: describeSource(t, c.shadowed),
                      })}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
      <div className="statusPanelActions" style={{ display: "flex", gap: 6 }}>
        <Button
          size="sm"
          variant="outline"
          className="h-7 text-xs px-2.5"
          onClick={() => setExpanded((v) => !v)}
          disabled={total === 0}
        >
          {expanded
            ? t("status.skillConflicts.collapse")
            : t("status.skillConflicts.expand")}
        </Button>
        {total > 0 && (
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs px-2.5"
            onClick={clearConflicts}
            disabled={clearing || loading}
            title={t("status.skillConflicts.clearHint")}
          >
            {clearing ? <RefreshCw size={12} className="animate-spin" /> : <XCircle size={12} />}
            {clearing ? t("status.skillConflicts.clearing") : t("status.skillConflicts.clear")}
          </Button>
        )}
        <Button
          size="sm"
          variant="outline"
          className="h-7 text-xs px-2.5"
          onClick={() => refresh(true)}
          disabled={loading || clearing}
        >
          {loading ? <RefreshCw size={12} className="animate-spin" /> : <CheckCircle2 size={12} />}
          {loading
            ? t("status.skillConflicts.refreshing")
            : t("status.skillConflicts.refresh")}
        </Button>
      </div>
    </div>
  );
}
