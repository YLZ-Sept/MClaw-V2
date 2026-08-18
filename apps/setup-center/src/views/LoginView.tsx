// ─── LoginView: Web access password login page ───

import { useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { login } from "../platform/auth";
import { IS_CAPACITOR } from "../platform/detect";
import { IconLink } from "../icons";
import logoUrl from "../assets/logo-new.png";

export function LoginView({
  apiBaseUrl,
  onLoginSuccess,
  onSwitchServer,
  onPreview,
}: {
  apiBaseUrl: string;
  onLoginSuccess: () => void;
  onSwitchServer?: () => void;
  onPreview?: () => void;
}) {
  const { t } = useTranslation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = useCallback(async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!username.trim() || !password.trim()) return;
    setLoading(true);
    setError(null);

    const result = await login(username.trim(), password, apiBaseUrl);
    setLoading(false);

    if (result.success) {
      onLoginSuccess();
    } else {
      const raw = (result.error || "").toLowerCase();
      if (raw.includes("too many")) {
        setError(t("login.tooManyAttempts"));
      } else if (raw.includes("invalid password")) {
        setError(t("login.invalidPassword"));
      } else if (raw.includes("abort") || raw.includes("timeout")) {
        setError(t("login.timeout"));
      } else if (raw.includes("failed to fetch") || raw.includes("networkerror") || raw.includes("fetch failed") || raw.includes("network") || raw.includes("load failed")) {
        setError(IS_CAPACITOR ? t("login.networkErrorMobile") : t("login.networkError"));
      } else {
        setError(result.error || t("login.failed"));
      }
    }
  }, [username, password, apiBaseUrl, onLoginSuccess, t]);

  const serverDisplay = apiBaseUrl ? apiBaseUrl.replace(/^https?:\/\//, "") : "";

  return (
    <>
      <style>{`
        .login-screen {
          position: relative;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          height: 100vh;
          width: 100vw;
          padding: 32px;
          padding-top: max(32px, env(safe-area-inset-top));
          box-sizing: border-box;
          overflow: hidden;
          background:
            radial-gradient(1200px 800px at 12% -12%, rgba(99,102,241,0.22), transparent 60%),
            radial-gradient(1000px 720px at 112% 112%, rgba(168,85,247,0.20), transparent 62%),
            linear-gradient(160deg, #070a14 0%, #0d1220 45%, #141129 100%);
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
          color: #e6e9f2;
        }
        .login-glow {
          position: absolute;
          border-radius: 50%;
          filter: blur(90px);
          opacity: 0.5;
          pointer-events: none;
        }
        .login-glow-a { width: 420px; height: 420px; background: rgba(99,102,241,0.35); top: -120px; left: -80px; }
        .login-glow-b { width: 360px; height: 360px; background: rgba(168,85,247,0.30); bottom: -120px; right: -60px; }

        .login-card {
          position: relative;
          z-index: 1;
          width: 100%;
          max-width: 400px;
          padding: 44px 44px 40px;
          box-sizing: border-box;
          text-align: center;
          border-radius: 22px;
          background: rgba(255,255,255,0.04);
          border: 1px solid rgba(255,255,255,0.09);
          backdrop-filter: blur(18px);
          -webkit-backdrop-filter: blur(18px);
          box-shadow: 0 24px 70px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.06);
        }

        .login-logo-wrap {
          position: relative;
          width: 76px;
          height: 76px;
          margin: 0 auto 18px;
        }
        .login-logo {
          width: 76px;
          height: 76px;
          border-radius: 18px;
          display: block;
          box-shadow: 0 8px 30px rgba(99,102,241,0.45);
          border: 1px solid rgba(255,255,255,0.12);
        }
        .login-logo-wrap::after {
          content: "";
          position: absolute;
          inset: -14px;
          border-radius: 50%;
          background: radial-gradient(circle, rgba(99,102,241,0.35), transparent 70%);
          filter: blur(10px);
          z-index: -1;
        }

        .login-title {
          margin: 0;
          font-size: 24px;
          font-weight: 700;
          letter-spacing: 0.5px;
          color: #f4f6fb;
        }
        .login-subtitle {
          margin: 8px 0 28px;
          font-size: 13px;
          color: rgba(230,233,242,0.55);
          letter-spacing: 1px;
        }

        .login-field {
          width: 100%;
          padding: 12px 16px;
          font-size: 15px;
          border-radius: 12px;
          border: 1px solid rgba(255,255,255,0.10);
          background: rgba(255,255,255,0.05);
          color: #e6e9f2;
          outline: none;
          box-sizing: border-box;
          margin-bottom: 14px;
          transition: border-color 0.18s, background 0.18s, box-shadow 0.18s;
        }
        .login-field::placeholder { color: rgba(230,233,242,0.4); }
        .login-field:focus {
          border-color: rgba(129,140,248,0.7);
          background: rgba(255,255,255,0.07);
          box-shadow: 0 0 0 4px rgba(99,102,241,0.18);
        }

        .login-error {
          background: rgba(244,63,94,0.12);
          border: 1px solid rgba(244,63,94,0.25);
          color: #fda4af;
          border-radius: 12px;
          padding: 10px 14px;
          font-size: 13px;
          margin-bottom: 16px;
          text-align: left;
          white-space: pre-line;
          line-height: 1.6;
        }

        .login-btn {
          width: 100%;
          border: none;
          border-radius: 12px;
          padding: 13px 0;
          font-size: 15px;
          font-weight: 600;
          letter-spacing: 1px;
          color: #fff;
          cursor: pointer;
          background: linear-gradient(135deg, #6366f1 0%, #7c3aed 100%);
          box-shadow: 0 8px 24px rgba(99,102,241,0.35);
          transition: transform 0.12s, box-shadow 0.2s, opacity 0.15s;
        }
        .login-btn:hover:not(:disabled) {
          transform: translateY(-1px);
          box-shadow: 0 12px 30px rgba(99,102,241,0.5);
        }
        .login-btn:active:not(:disabled) { transform: translateY(0) scale(0.98); }
        .login-btn:disabled {
          cursor: wait;
          opacity: 0.65;
          background: linear-gradient(135deg, #475569 0%, #334155 100%);
          box-shadow: none;
        }

        .login-link {
          width: 100%;
          margin-top: 14px;
          background: none;
          border: 1px solid rgba(255,255,255,0.10);
          border-radius: 12px;
          padding: 11px 0;
          font-size: 14px;
          color: rgba(230,233,242,0.65);
          cursor: pointer;
          transition: border-color 0.15s, color 0.15s;
        }
        .login-link:hover {
          border-color: rgba(129,140,248,0.6);
          color: #c7d2fe;
        }

        .login-server {
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 6px;
          margin-bottom: 18px;
          padding: 8px 12px;
          border-radius: 10px;
          background: rgba(255,255,255,0.05);
          border: 1px solid rgba(255,255,255,0.08);
          font-size: 12px;
          color: rgba(230,233,242,0.55);
        }
      `}</style>

      <div className="login-screen">
        <div className="login-glow login-glow-a" />
        <div className="login-glow login-glow-b" />

        <form className="login-card" onSubmit={handleSubmit}>
          <div className="login-logo-wrap">
            <img className="login-logo" src={logoUrl} alt="MClaw" />
          </div>
          <h2 className="login-title">{t("brand.title")}</h2>
          <p className="login-subtitle">{t("brand.sub")}</p>

          {/* Server address display for Capacitor */}
          {IS_CAPACITOR && serverDisplay && (
            <div className="login-server">
              <IconLink size={13} style={{ opacity: 0.7, flexShrink: 0 }} />
              <span style={{ fontFamily: "monospace", wordBreak: "break-all" }}>{serverDisplay}</span>
            </div>
          )}

          {error && <div className="login-error">{error}</div>}

          <input
            className="login-field"
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder={t("login.usernamePlaceholder")}
            autoFocus
            autoComplete="username"
            disabled={loading}
          />
          <input
            className="login-field"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={t("login.passwordPlaceholder")}
            disabled={loading}
          />

          <button
            className="login-btn"
            type="submit"
            disabled={loading || !username.trim() || !password.trim()}
          >
            {loading ? t("login.loggingIn") : t("login.submit")}
          </button>

          {/* Switch server button for Capacitor */}
          {onSwitchServer && (
            <button
              className="login-link"
              type="button"
              onClick={onSwitchServer}
            >
              {t("login.switchServer", { defaultValue: "切换 / 添加服务器" })}
            </button>
          )}
        </form>
      </div>
    </>
  );
}
