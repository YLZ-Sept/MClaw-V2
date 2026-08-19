import { Fragment, useState, useCallback, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import type { StepId, Step, ViewId, PluginUIApp } from "../types";
import {
  IconChat, IconIM, IconSkills, IconStatus, IconConfig,
  IconChevronDown, IconChevronRight,
  IconZap, IconPlug, IconCalendar,
  IconBrain, IconUsers, IconBot,
  IconGear, IconBook, IconStorefront, IconPuzzle, IconFingerprint, IconLayoutGrid,
  IconShield, IconRadar, IconBuilding, IconBarChart,
} from "../icons";
import logoUrl from "../assets/logo-new.png";
import { ReleaseNotesDialog, normalizeReleaseVersion } from "./ReleaseNotesDialog";

export type SidebarProps = {
  collapsed: boolean;
  onToggleCollapsed: () => void;
  view: ViewId;
  onViewChange: (v: ViewId) => void;
  configExpanded: boolean;
  onToggleConfig: () => void;
  steps: Step[];
  stepId: StepId;
  onStepChange: (id: StepId) => void;
  disabledViews: string[];
  storeVisible: boolean;
  desktopVersion: string;
  backendVersion: string | null;
  serviceRunning: boolean;
  onRefreshStatus: () => Promise<void>;
  isWeb?: boolean;
  mobileOpen?: boolean;
  httpApiBase?: string;
  unreadFeedbackCount?: number;
  pendingApprovalsCount?: number;
  onCheckForUpdate?: () => Promise<void>;
  updateCheckPending?: boolean;
  username?: string;
  onLogout?: () => void;
};

const stepIcons: Partial<Record<StepId, React.ReactNode>> = {
  llm: <IconZap size={14} />,
  im: <IconIM size={14} />,
  tools: <IconSkills size={14} />,
  agent: <IconBot size={14} />,
  workspace: <IconBook size={14} />,
  advanced: <IconGear size={14} />,
};

function StepDot({ stepId: sid }: { stepId: StepId }) {
  return <div className="stepDot">{stepIcons[sid]}</div>;
}

type NavGroupId = "capabilities" | "apps" | "monitor" | "multiAgent" | "store";
const GROUP_ICON_SIZE = 16;

// Kill-switch for the sidebar "Apps" group (Plugin 2.0 UI apps). Hiding it
// only removes the sidebar entries — the plugins' tools/routes/hooks keep
// running. Flip to `true` to restore the group.
const SHOW_APPS_GROUP = false;

const BETA_SUP = <sup style={{ fontSize: 9, color: "var(--primary, #3b82f6)", fontWeight: 600 }}>Beta</sup>;

function NavGroupHeader({
  collapsed: sidebarCollapsed,
  icon,
  label,
  expanded,
  onToggle,
}: {
  collapsed: boolean;
  icon: React.ReactNode;
  label: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="navGroupHeader" onClick={onToggle} role="button" tabIndex={0} title={sidebarCollapsed ? label : undefined}>
      {!sidebarCollapsed ? (
        <>
          <span className="navGroupLabelWrap">
            <span className="navGroupIcon">{icon}</span>
            <span className="navGroupLabel">{label}</span>
          </span>
          <span className="navGroupChevron">
            {expanded ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
          </span>
        </>
      ) : (
        <span className="navGroupIcon navGroupIconCollapsed">{icon}</span>
      )}
    </div>
  );
}

export function Sidebar({
  collapsed, onToggleCollapsed,
  view, onViewChange,
  configExpanded, onToggleConfig,
  steps, stepId, onStepChange,
  disabledViews,
  storeVisible,
  desktopVersion, backendVersion, serviceRunning,
  onRefreshStatus, isWeb, mobileOpen, httpApiBase,
  unreadFeedbackCount, pendingApprovalsCount,
  onCheckForUpdate, updateCheckPending = false,
  username, onLogout,
}: SidebarProps) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  // Pick a localized plugin app title from `title_i18n`, falling back to the
  // default `title` string. Mirror of pickI18n() in PluginManagerView so the
  // sidebar and the manager list always show the same label per language.
  const pickAppTitle = (app: PluginUIApp): string => {
    const dict = app.title_i18n;
    if (dict && typeof dict === "object") {
      if (dict[lang]) return dict[lang];
      const base = lang.split("-")[0];
      if (base && dict[base]) return dict[base];
      if (dict.en) return dict.en;
      const first = Object.values(dict).find(v => typeof v === "string" && v);
      if (first) return first;
    }
    return app.title;
  };

  const [expandedGroups, setExpandedGroups] = useState<Record<NavGroupId, boolean>>({
    capabilities: false,
    apps: false,
    monitor: false,
    multiAgent: false,
    store: false,
  });

  const toggleGroup = useCallback((id: NavGroupId) => {
    setExpandedGroups(prev => ({ ...prev, [id]: !prev[id] }));
  }, []);

  const [pluginApps, setPluginApps] = useState<PluginUIApp[]>([]);
  const [releaseNotesOpen, setReleaseNotesOpen] = useState(false);
  const releaseNotesVersion = normalizeReleaseVersion(desktopVersion);

  // Refetch the Apps sidebar list. Triggered initially, when backend
  // availability changes, and on the global "mclaw:plugin-apps-changed"
  // event dispatched by PluginManagerView after install/enable/disable/etc.
  //
  // Tauri can mark the backend process as "running" before FastAPI has mounted
  // plugin UI routes. Use sparse startup retries as a fallback; the main
  // trigger is the backend-ready event dispatched after /api/health succeeds.
  useEffect(() => {
    if (!SHOW_APPS_GROUP || !httpApiBase || !serviceRunning) { setPluginApps([]); return; }
    let cancelled = false;
    const retryDelays = [2_000, 8_000, 20_000, 60_000, 120_000];
    const timers = new Set<ReturnType<typeof setTimeout>>();

    const clearTimers = () => {
      timers.forEach(timer => clearTimeout(timer));
      timers.clear();
    };

    const scheduleRetry = (attempt: number) => {
      const delay = retryDelays[attempt];
      if (delay == null) return false;
      const timer = setTimeout(() => {
        timers.delete(timer);
        void refetch(attempt + 1);
      }, delay);
      timers.add(timer);
      return true;
    };

    const refetch = async (attempt = 0) => {
      try {
        const r = await fetch(`${httpApiBase}/api/plugins/ui-apps`);
        const data = r.ok ? await r.json() : [];
        if (cancelled) return;
        const apps = Array.isArray(data) ? data : [];
        setPluginApps(apps);
        if (apps.length === 0) scheduleRetry(attempt);
      } catch {
        if (cancelled) return;
        if (!scheduleRetry(attempt)) setPluginApps([]);
      }
    };

    refetch();
    const onChanged = () => {
      clearTimers();
      void refetch();
    };
    window.addEventListener("mclaw:plugin-apps-changed", onChanged);
    return () => {
      cancelled = true;
      clearTimers();
      window.removeEventListener("mclaw:plugin-apps-changed", onChanged);
    };
  }, [httpApiBase, serviceRunning]);

  const capViews: ViewId[] = ["skills", "mcp", "plugins", "memory", "scheduler"];
  const monViews: ViewId[] = ["token_stats", "skill_usage", "security", "pending_approvals"];
  const maViews: ViewId[] = ["dashboard", "org_editor", "pixel_office", "agent_manager"];
  const stViews: ViewId[] = ["agent_store", "skill_store"];

  const prevViewRef = useRef(view);
  useEffect(() => {
    if (prevViewRef.current === view) return;
    prevViewRef.current = view;
    const groupOf = (v: ViewId): NavGroupId | null =>
      capViews.includes(v) ? "capabilities"
        : monViews.includes(v) ? "monitor"
        : maViews.includes(v) ? "multiAgent"
        : stViews.includes(v) ? "store"
        : (typeof v === "string" && v.startsWith("plugin_app:")) ? "apps"
        : null;
    const g = groupOf(view);
    if (g) setExpandedGroups(prev => ({ ...prev, [g]: true }));
  }, [view]);

  const capExpanded = expandedGroups.capabilities;
  const appsExpanded = expandedGroups.apps;
  const monExpanded = expandedGroups.monitor;
  const maExpanded = expandedGroups.multiAgent;
  const stExpanded = expandedGroups.store;

  return (
    <aside className={`sidebar ${collapsed ? "sidebarCollapsed" : ""}${mobileOpen ? " sidebarOpen" : ""}`}>
      <div className="sidebarHeader">
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <img
            src={logoUrl}
            alt="Mclaw"
            className="brandLogo"
            onClick={onToggleCollapsed}
            style={{ cursor: "pointer" }}
            title={collapsed ? t("sidebar.expand") : t("sidebar.collapse")}
          />
          {!collapsed && (
            <div>
              <div className="brandTitle">{t("brand.title")}</div>
              <div className="brandSub">{t("brand.sub")}</div>
            </div>
          )}
        </div>
      </div>

      <div className="sidebarNav">
        {/* ── Primary: always visible ── */}
        <div className={`navItem ${view === "chat" ? "navItemActive" : ""}`} onClick={() => onViewChange("chat")} role="button" tabIndex={0} title={t("sidebar.chat")}>
          <IconChat size={16} /> {!collapsed && <span>{t("sidebar.chat")}</span>}
        </div>
        {!disabledViews.includes("im") && (
          <div className={`navItem ${view === "im" ? "navItemActive" : ""}`} onClick={() => onViewChange("im")} role="button" tabIndex={0} title={t("sidebar.im")}>
            <IconIM size={16} /> {!collapsed && <span>{t("sidebar.im")}</span>}
          </div>
        )}
        <div className={`navItem ${view === "status" ? "navItemActive" : ""}`} onClick={async () => { onViewChange("status"); try { await onRefreshStatus(); } catch { /* ignore */ } }} role="button" tabIndex={0} title={t("sidebar.status")}>
          <IconStatus size={16} /> {!collapsed && <span>{t("sidebar.status")}</span>}
        </div>

        {/* ── Group: Capabilities ── */}
        <NavGroupHeader collapsed={collapsed} icon={<IconPuzzle size={GROUP_ICON_SIZE} />} label={t("sidebar.groupCapabilities")} expanded={capExpanded} onToggle={() => toggleGroup("capabilities")} />
        {(collapsed || capExpanded) && (
          <div className="navGroupItems">
            {!disabledViews.includes("skills") && (
              <div className={`navItem ${view === "skills" ? "navItemActive" : ""}`} onClick={() => onViewChange("skills")} role="button" tabIndex={0} title={t("sidebar.skills")}>
                <IconSkills size={16} /> {!collapsed && <span>{t("sidebar.skills")}</span>}
              </div>
            )}
            {!disabledViews.includes("mcp") && (
              <div className={`navItem ${view === "mcp" ? "navItemActive" : ""}`} onClick={() => onViewChange("mcp")} role="button" tabIndex={0} title="MCP">
                <IconPlug size={16} /> {!collapsed && <span>MCP</span>}
              </div>
            )}
            <div className={`navItem ${view === "plugins" ? "navItemActive" : ""}`} onClick={() => onViewChange("plugins")} role="button" tabIndex={0} title={t("sidebar.plugins")}>
              <IconPuzzle size={16} /> {!collapsed && <span>{t("sidebar.plugins")} {BETA_SUP}</span>}
            </div>
            <div className={`navItem ${view === "memory" ? "navItemActive" : ""}`} onClick={() => onViewChange("memory")} role="button" tabIndex={0} title={t("sidebar.memory")}>
              <IconBrain size={16} /> {!collapsed && <span>{t("sidebar.memory")}</span>}
            </div>
            <div className={`navItem ${view === "scheduler" ? "navItemActive" : ""}`} onClick={() => onViewChange("scheduler")} role="button" tabIndex={0} title={t("sidebar.scheduler")}>
              <IconCalendar size={16} /> {!collapsed && <span>{t("sidebar.scheduler")}</span>}
            </div>
          </div>
        )}

        {/* ── Group: Apps (Plugin 2.0 UI plugins) — gated by SHOW_APPS_GROUP ── */}
        {SHOW_APPS_GROUP && pluginApps.length > 0 && (
          <>
            <NavGroupHeader collapsed={collapsed} icon={<IconLayoutGrid size={GROUP_ICON_SIZE} />} label={t("sidebar.groupApps", "Apps")} expanded={appsExpanded} onToggle={() => toggleGroup("apps")} />
            {(collapsed || appsExpanded) && (
              <div className="navGroupItems">
                {pluginApps.map(app => {
                  const appViewId: ViewId = `plugin_app:${app.id}`;
                  const appTitle = pickAppTitle(app);
                  return (
                    <div
                      key={app.id}
                      className={`navItem ${view === appViewId ? "navItemActive" : ""}`}
                      onClick={() => onViewChange(appViewId)}
                      role="button"
                      tabIndex={0}
                      title={appTitle}
                    >
                      {app.icon_url ? (
                        <img src={`${httpApiBase}${app.icon_url}`} alt="" style={{ width: 16, height: 16, borderRadius: 2 }} />
                      ) : (
                        <IconLayoutGrid size={16} />
                      )}
                      {!collapsed && <span>{appTitle}</span>}
                    </div>
                  );
                })}
              </div>
            )}
          </>
        )}

        {/* ── Group: Monitor ── */}
        <NavGroupHeader collapsed={collapsed} icon={<IconRadar size={GROUP_ICON_SIZE} />} label={t("sidebar.groupMonitor")} expanded={monExpanded} onToggle={() => toggleGroup("monitor")} />
        {(collapsed || monExpanded) && (
          <div className="navGroupItems">
            <div className={`navItem ${view === "token_stats" ? "navItemActive" : ""}`} onClick={() => onViewChange("token_stats")} role="button" tabIndex={0} title={t("sidebar.tokenStats")} style={disabledViews.includes("token_stats") ? { opacity: 0.4 } : undefined}>
              <IconZap size={16} /> {!collapsed && <span>{t("sidebar.tokenStats")}</span>}
            </div>
            <div className={`navItem ${view === "skill_usage" ? "navItemActive" : ""}`} onClick={() => onViewChange("skill_usage")} role="button" tabIndex={0} title={t("sidebar.skillUsage")}>
              <IconBarChart size={16} /> {!collapsed && <span>{t("sidebar.skillUsage")}</span>}
            </div>
            <div className={`navItem ${view === "security" ? "navItemActive" : ""}`} onClick={() => onViewChange("security")} role="button" tabIndex={0} title={t("sidebar.security")}>
              <IconShield size={16} /> {!collapsed && <span>{t("sidebar.security")}</span>}
            </div>
            <div className={`navItem ${view === "pending_approvals" ? "navItemActive" : ""}`} onClick={() => onViewChange("pending_approvals")} role="button" tabIndex={0} title={t("sidebar.pendingApprovals")} style={{ position: "relative" }}>
              <IconFingerprint size={16} /> {!collapsed && <span>{t("sidebar.pendingApprovals")}</span>}
              {(pendingApprovalsCount ?? 0) > 0 && (
                <span style={{
                  position: "absolute", top: 4, left: collapsed ? 22 : undefined, right: collapsed ? undefined : 8,
                  minWidth: 16, height: 16, borderRadius: 8,
                  background: "#ef4444", color: "#fff", fontSize: 10, fontWeight: 600,
                  display: "flex", alignItems: "center", justifyContent: "center", padding: "0 4px",
                }}>{pendingApprovalsCount}</span>
              )}
            </div>
          </div>
        )}

        {/* ── Group: Multi-Agent ── */}
        <NavGroupHeader collapsed={collapsed} icon={<IconBot size={GROUP_ICON_SIZE} />} label={t("sidebar.groupMultiAgent")} expanded={maExpanded} onToggle={() => toggleGroup("multiAgent")} />
        {(collapsed || maExpanded) && (
          <div className="navGroupItems">
            <div className={`navItem ${view === "dashboard" ? "navItemActive" : ""}`} onClick={() => onViewChange("dashboard")} role="button" tabIndex={0} title={t("sidebar.dashboard")}>
              <IconUsers size={16} /> {!collapsed && <span>{t("sidebar.dashboard")} {BETA_SUP}</span>}
            </div>
            <div className={`navItem ${view === "org_editor" ? "navItemActive" : ""}`} onClick={() => onViewChange("org_editor")} role="button" tabIndex={0} title={t("sidebar.orgEditor")}>
              <IconLayoutGrid size={16} /> {!collapsed && <span>{t("sidebar.orgEditor")} {BETA_SUP}</span>}
            </div>
            <div className={`navItem ${view === "pixel_office" ? "navItemActive" : ""}`} onClick={() => onViewChange("pixel_office")} role="button" tabIndex={0} title={t("sidebar.pixelOffice")}>
              <IconBuilding size={16} /> {!collapsed && <span>{t("sidebar.pixelOffice")} {BETA_SUP}</span>}
            </div>
            <div className={`navItem ${view === "agent_manager" ? "navItemActive" : ""}`} onClick={() => onViewChange("agent_manager")} role="button" tabIndex={0} title={t("sidebar.agentManager")}>
              <IconBot size={16} /> {!collapsed && <span>{t("sidebar.agentManager")}</span>}
            </div>
          </div>
        )}

        {/* ── Knowledge Base ── */}
        <div className={`navItem ${view === "knowledge" ? "navItemActive" : ""}`} onClick={() => onViewChange("knowledge")} role="button" tabIndex={0} title={t("sidebar.knowledge")}>
          <IconBook size={16} /> {!collapsed && <span>{t("sidebar.knowledge")}</span>}
        </div>

        {/* ── Group: Store ── */}
        {storeVisible && (
          <>
            <NavGroupHeader collapsed={collapsed} icon={<IconStorefront size={GROUP_ICON_SIZE} />} label={t("sidebar.groupStore")} expanded={stExpanded} onToggle={() => toggleGroup("store")} />
            {(collapsed || stExpanded) && (
              <div className="navGroupItems">
                <div className={`navItem ${view === "agent_store" ? "navItemActive" : ""}`} onClick={() => onViewChange("agent_store")} role="button" tabIndex={0} title={t("sidebar.agentStore")}>
                  <IconStorefront size={16} /> {!collapsed && <span>{t("sidebar.agentStore")} {BETA_SUP}</span>}
                </div>
                <div className={`navItem ${view === "skill_store" ? "navItemActive" : ""}`} onClick={() => onViewChange("skill_store")} role="button" tabIndex={0} title={t("sidebar.skillStore")}>
                  <IconPuzzle size={16} /> {!collapsed && <span>{t("sidebar.skillStore")} {BETA_SUP}</span>}
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {/* Collapsible Config section */}
      <div className="configSection">
        <div className="configHeader" onClick={onToggleConfig} role="button" tabIndex={0} title={t("sidebar.config")}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <IconConfig size={16} />
            {!collapsed && <span>{t("sidebar.config")}</span>}
          </div>
          {!collapsed && (
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              {configExpanded ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
            </div>
          )}
        </div>
        {!collapsed && configExpanded && (
          <div className="stepList">
            {steps.map((s) => {
              const isActive = view === "wizard" && s.id === stepId;
              return (
                <Fragment key={s.id}>
                  <div
                    className={`stepItem ${isActive ? "stepItemActive" : ""}`}
                    onClick={() => { onViewChange("wizard"); onStepChange(s.id); }}
                    role="button" tabIndex={0}
                  >
                    <StepDot stepId={s.id} />
                    <div className="stepMeta"><div className="stepTitle">{s.title}</div></div>
                  </div>
                  {s.id === "agent" && (
                    <div
                      className={`stepItem ${view === "identity" ? "stepItemActive" : ""}`}
                      onClick={() => onViewChange("identity")}
                      role="button" tabIndex={0}
                      title={t("sidebar.identity")}
                    >
                      <div className="stepDot"><IconFingerprint size={14} /></div>
                      <div className="stepMeta"><div className="stepTitle">{t("sidebar.identity")}</div></div>
                    </div>
                  )}
                </Fragment>
              );
            })}
          </div>
        )}
      </div>

      {/* Version info + website and feedback links at sidebar bottom */}
      {/* ── User status row ── */}
      {username && (
        <div style={{
          padding: "6px 12px",
          borderTop: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          flexShrink: 0,
        }}>
          <span style={{ fontSize: 12, color: "var(--muted)", display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{
              width: 22, height: 22, borderRadius: "50%",
              background: "var(--primary, #2563eb)",
              color: "#fff", fontSize: 11, fontWeight: 600,
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              flexShrink: 0,
            }}>
              {username[0].toUpperCase()}
            </span>
            {!collapsed && <span>{username}</span>}
          </span>
          {!collapsed && onLogout && (
            <button
              type="button"
              onClick={onLogout}
              title="退出登录"
              style={{
                fontSize: 11, color: "var(--muted)", opacity: 0.6,
                background: "none", border: "none", cursor: "pointer",
                padding: "2px 6px", borderRadius: 4,
              }}
            >
              退出
            </button>
          )}
        </div>
      )}

      {releaseNotesOpen && (
        <ReleaseNotesDialog
          version={releaseNotesVersion}
          onClose={() => setReleaseNotesOpen(false)}
        />
      )}
    </aside>
  );
}
