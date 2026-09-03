// ─── LicenseInfoView: 授权状态 / 产品信息 / 续费入口 ───
//
// 与 LicenseView 的分工：LicenseView 是**阻断态**的激活页（未激活、
// 过期、换机时全屏顶掉应用）；本页是**已激活**状态下的常驻信息页，
// 挂在「配置 → 高级配置 → 系统信息与运维」下，随时可查到期时间、
// 机器码与续费联系方式。
//
// 不放进收件箱（Inbox）：那是服务端签名广播通道，专发产品公告，
// 混入本机授权状态会让"消息"这个概念同时指两种来源完全不同的东西。
//
// 配色一律走主题语义色（bg-card / text-muted-foreground / border-border
// …），不硬编码十六进制，否则暗色主题下会出现白底黑字的色块。

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  fetchFingerprint,
  fetchLicenseStatus,
  type FingerprintInfo,
  type LicenseStatus,
} from "../platform/license";
import { openExternalUrl } from "../platform";
import { VENDOR } from "../vendor";
import { Button } from "../components/ui/button";
import {
  IconShield, IconKey, IconMail, IconGlobe, IconClock,
  IconAlertCircle, IconRefresh, IconUsers,
} from "../icons";

/** 状态紧迫度 → 主题内的语义配色。 */
const TONE = {
  ok: {
    ring: "border-emerald-500/30 bg-emerald-500/[0.07]",
    badge: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
    icon: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
    strong: "text-emerald-600 dark:text-emerald-400",
  },
  warn: {
    ring: "border-amber-500/35 bg-amber-500/[0.07]",
    badge: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
    icon: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
    strong: "text-amber-600 dark:text-amber-400",
  },
  danger: {
    ring: "border-destructive/40 bg-destructive/[0.07]",
    badge: "bg-destructive/15 text-destructive",
    icon: "bg-destructive/15 text-destructive",
    strong: "text-destructive",
  },
  neutral: {
    ring: "border-border bg-muted/30",
    badge: "bg-muted text-muted-foreground",
    icon: "bg-muted text-muted-foreground",
    strong: "text-foreground",
  },
} as const;

export function LicenseInfoView({
  apiBaseUrl,
  backendVersion,
  onReactivate,
}: {
  apiBaseUrl: string;
  backendVersion?: string;
  onReactivate?: () => void;
}) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<LicenseStatus | null>(null);
  const [fingerprint, setFingerprint] = useState<FingerprintInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    const [s, f] = await Promise.all([
      fetchLicenseStatus(apiBaseUrl),
      fetchFingerprint(apiBaseUrl),
    ]);
    setStatus(s);
    setFingerprint(f);
    setLoading(false);
  }, [apiBaseUrl]);

  useEffect(() => { void load(); }, [load]);

  const handleCopy = useCallback(async () => {
    if (!fingerprint) return;
    try {
      await navigator.clipboard.writeText(fingerprint.fingerprint);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch { /* 剪贴板不可用时仍可手动选中（select-all） */ }
  }, [fingerprint]);

  if (loading) {
    return (
      <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
        <span className="spinner size-3.5" />
        {t("licenseInfo.loading")}
      </div>
    );
  }

  // status 为 null 表示后端没读到（服务未起/网络问题），不等于未授权——
  // 如实说"读不到"，不要显示成"未激活"吓客户。
  if (!status) {
    return (
      <div className="flex flex-col items-center gap-3 py-10 text-sm text-muted-foreground">
        <span>{t("licenseInfo.unreachable")}</span>
        <Button variant="outline" size="sm" onClick={() => void load()}>
          <IconRefresh size={14} /> {t("licenseInfo.retry")}
        </Button>
      </div>
    );
  }

  const st = status.state;
  const disabled = st === "disabled";
  const days = status.days_remaining ?? 0;

  const tone =
    disabled ? TONE.neutral
      : st === "grace" || st === "expired" || st === "mismatch" || st === "invalid" ? TONE.danger
      : st === "active" && days <= 30 ? TONE.warn
      : st === "active" ? TONE.ok
      : TONE.danger;

  const stateLabel = disabled
    ? t("licenseInfo.stateDisabled")
    : t(`licenseInfo.state.${st}`, status.message);

  return (
    <div className="flex flex-col gap-3">

      {/* ── 状态总览 ── */}
      <div className={`rounded-xl border p-4 ${tone.ring}`}>
        <div className="flex items-start gap-3">
          <span className={`inline-flex size-9 shrink-0 items-center justify-center rounded-lg ${tone.icon}`}>
            {tone === TONE.ok ? <IconShield size={18} /> : <IconAlertCircle size={18} />}
          </span>

          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className={`text-[15px] font-semibold ${tone.strong}`}>{stateLabel}</span>
              {!disabled && status.expires && (
                <span className={`rounded-md px-1.5 py-0.5 text-[11px] font-semibold ${tone.badge}`}>
                  {days > 0
                    ? t("licenseInfo.daysLeft", { days })
                    : days === 0
                      ? t("licenseInfo.expiresToday")
                      : t("licenseInfo.overdueDays", { days: -days })}
                </span>
              )}
            </div>

            {!disabled && status.expires && (
              <div className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
                <IconClock size={13} />
                <span>{t("licenseInfo.expiresOn", { date: status.expires })}</span>
              </div>
            )}

            {/* 宽限期最需要把后果说清：还能用，但有倒计时。 */}
            {st === "grace" && (
              <p className="mt-2 rounded-lg border border-destructive/30 bg-destructive/[0.06] px-2.5 py-1.5 text-xs leading-relaxed text-destructive">
                {t("licenseInfo.graceNote", { days: status.grace_period_days ?? 7 })}
              </p>
            )}
          </div>

          <Button variant="ghost" size="icon-sm" onClick={() => void load()} title={t("licenseInfo.retry")}>
            <IconRefresh size={14} />
          </Button>
        </div>

        {/* 授权详情：紧贴状态，同一张卡里读完 */}
        {!disabled && (status.customer || status.serial) && (
          <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 border-t border-border/60 pt-3 text-sm">
            {status.customer && (
              <>
                <dt className="whitespace-nowrap text-xs text-muted-foreground">{t("license.fieldCustomer")}</dt>
                <dd className="m-0 font-medium break-words">{status.customer}</dd>
              </>
            )}
            {status.serial && (
              <>
                <dt className="whitespace-nowrap text-xs text-muted-foreground">{t("license.fieldSerial")}</dt>
                <dd className="m-0 font-mono text-[13px] font-medium">{status.serial}</dd>
              </>
            )}
            {status.issued && (
              <>
                <dt className="whitespace-nowrap text-xs text-muted-foreground">{t("licenseInfo.issued")}</dt>
                <dd className="m-0 font-medium">{status.issued}</dd>
              </>
            )}
            <>
              <dt className="whitespace-nowrap text-xs text-muted-foreground">{t("license.fieldMaxUsers")}</dt>
              <dd className="m-0 flex items-center gap-1.5 font-medium">
                <IconUsers size={13} className="text-muted-foreground" />
                {!status.max_users || status.max_users <= 0
                  ? t("license.usersUnlimited")
                  : t("licenseInfo.usersCount", { count: status.max_users })}
              </dd>
            </>
            {status.tier && (
              <>
                <dt className="whitespace-nowrap text-xs text-muted-foreground">{t("license.fieldTier")}</dt>
                <dd className="m-0 font-medium">{status.tier}</dd>
              </>
            )}
          </dl>
        )}
      </div>

      {/* ── 产品信息 ── */}
      <div className="rounded-xl border border-border/80 bg-card/60 p-4">
        <div className="flex items-baseline gap-2">
          <span className="text-[15px] font-semibold">{t("brand.title")}</span>
          {backendVersion && (
            <span className="font-mono text-xs text-muted-foreground">v{backendVersion}</span>
          )}
        </div>
        <p className="mt-1.5 text-[13px] leading-relaxed text-muted-foreground">
          {t("licenseInfo.productDesc")}
        </p>
      </div>

      {/* ── 续费与技术支持 ── */}
      <div className="rounded-xl border border-border/80 bg-card/60 p-4">
        <h4 className="m-0 mb-1 text-sm font-semibold">{t("licenseInfo.renewTitle")}</h4>
        <p className="m-0 mb-3 text-xs leading-relaxed text-muted-foreground">
          {t("licenseInfo.renewHint")}
        </p>

        {/* 机器码：续费/换机时供应商必须拿到这串，放在联系方式旁最顺手。 */}
        {fingerprint && (
          <div className="mb-3 rounded-lg border border-border/70 bg-muted/30 px-3 py-2.5">
            <div className="mb-1.5 text-xs font-medium text-muted-foreground">
              {t("license.fingerprintLabel")}
            </div>
            <div className="flex items-center gap-2">
              <code className="flex-1 select-all break-all font-mono text-sm tracking-wider">
                {fingerprint.fingerprint}
              </code>
              <Button variant="outline" size="xs" onClick={handleCopy}>
                {copied ? t("license.copied") : t("license.copy")}
              </Button>
            </div>
          </div>
        )}

        <div className="grid gap-2 text-[13px] sm:grid-cols-2">
          <ContactRow icon={<IconShield size={13} />} className="sm:col-span-2">
            <span className="font-medium">{VENDOR.company}</span>
          </ContactRow>

          {VENDOR.phone && (
            <ContactRow icon={<IconPhoneGlyph />}>
              <a href={`tel:${VENDOR.phone}`} className="text-primary hover:underline">
                {VENDOR.phone}
              </a>
            </ContactRow>
          )}
          {VENDOR.address && (
            <ContactRow icon={<IconPinGlyph />} className="sm:col-span-2">
              <span className="text-muted-foreground">{VENDOR.address}</span>
            </ContactRow>
          )}
          {VENDOR.email && (
            <ContactRow icon={<IconMail size={13} />}>
              <a href={`mailto:${VENDOR.email}`} className="text-primary hover:underline">
                {VENDOR.email}
              </a>
            </ContactRow>
          )}
          {VENDOR.website && (
            <ContactRow icon={<IconGlobe size={13} />}>
              <ExternalLink url={VENDOR.website}>
                {VENDOR.website.replace(/^https?:\/\//, "")}
              </ExternalLink>
            </ContactRow>
          )}
          {VENDOR.wechatService && (
            <ContactRow icon={<IconChatGlyph />}>
              <ExternalLink url={VENDOR.wechatService}>
                {t("licenseInfo.wechatService")}
              </ExternalLink>
            </ContactRow>
          )}
          {VENDOR.wechatAccount && (
            <ContactRow icon={<IconChatGlyph />}>
              <span className="text-muted-foreground">
                {t("licenseInfo.wechatAccount")}：{VENDOR.wechatAccount}
              </span>
            </ContactRow>
          )}
          {VENDOR.douyin && (
            <ContactRow icon={<IconVideoGlyph />}>
              <span className="text-muted-foreground">
                {t("licenseInfo.douyin")}：{VENDOR.douyin}
              </span>
            </ContactRow>
          )}
          {VENDOR.hours && (
            <ContactRow icon={<IconClock size={13} />}>
              <span className="text-muted-foreground">{VENDOR.hours}</span>
            </ContactRow>
          )}
        </div>

        {onReactivate && (
          <div className="mt-3 border-t border-border/60 pt-3">
            <Button variant="outline" size="sm" onClick={onReactivate}>
              <IconKey size={14} /> {t("licenseInfo.enterNewCode")}
            </Button>
            <p className="m-0 mt-1.5 text-[11px] text-muted-foreground">
              {t("licenseInfo.enterNewCodeHint")}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

function ContactRow({
  icon, children, className = "",
}: { icon: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <span className="inline-flex size-5 shrink-0 items-center justify-center rounded-md bg-muted/70 text-muted-foreground">
        {icon}
      </span>
      <span className="min-w-0 break-words">{children}</span>
    </div>
  );
}

function ExternalLink({ url, children }: { url: string; children: React.ReactNode }) {
  return (
    <a
      href={url}
      onClick={(e) => { e.preventDefault(); void openExternalUrl(url); }}
      className="text-primary hover:underline"
    >
      {children}
    </a>
  );
}

// icons.tsx 里没有这几个字形，就地补最小实现，避免为四个图标引整套库。
const glyph = { width: 13, height: 13, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round" } as const;

function IconPhoneGlyph() {
  return (
    <svg {...glyph}>
      <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.9.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z" />
    </svg>
  );
}

function IconPinGlyph() {
  return (
    <svg {...glyph}>
      <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" />
      <circle cx="12" cy="10" r="3" />
    </svg>
  );
}

function IconChatGlyph() {
  return (
    <svg {...glyph}>
      <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
    </svg>
  );
}

function IconVideoGlyph() {
  return (
    <svg {...glyph}>
      <path d="m22 8-6 4 6 4V8z" />
      <rect x="2" y="6" width="14" height="12" rx="2" ry="2" />
    </svg>
  );
}
