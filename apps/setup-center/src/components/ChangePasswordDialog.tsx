import { useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { safeFetch } from "../providers";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "./ui/dialog";

// Mirror of mclaw.api.routes.auth:_validate_password_strength (≥8 chars, not
// all digits, not all letters) — same rule as passwordValidationKey in
// AdvancedView, kept local so the dialog is self-contained.
function passwordValidationKey(pw: string): string | null {
  if (pw.length < 8) return "adv.userPasswordTooShort";
  if (/^\d+$/u.test(pw)) return "adv.userPasswordAllDigits";
  if (/^\p{L}+$/u.test(pw)) return "adv.userPasswordAllLetters";
  return null;
}

/**
 * Self-service password change for the signed-in user.
 *
 * Calls POST /api/auth/change-password with the Bearer token, so the backend
 * resolves "which user" from the token itself. The current password is only
 * verified server-side for remote callers; on loopback (desktop) it is not
 * required, hence the `requireCurrent` flag.
 *
 * On success the backend bumps token_version, invalidating every session —
 * callers should log the user out via `onDone`.
 */
export function ChangePasswordDialog({
  apiBase,
  requireCurrent = true,
  onClose,
  onDone,
}: {
  apiBase: string;
  requireCurrent?: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [currentPw, setCurrentPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [error, setError] = useState("");
  const [isBusy, setIsBusy] = useState(false);

  // Map backend error codes / detail strings to localized messages; unknown
  // values (safeFetch throws the raw `detail`) are shown verbatim.
  const localizedError = (detail: unknown): string => {
    const s = String(detail ?? "");
    switch (s) {
      case "password_too_short": return t("adv.userPasswordTooShort");
      case "password_all_digits": return t("adv.userPasswordAllDigits");
      case "password_all_letters": return t("adv.userPasswordAllLetters");
      case "password_invalid": return t("adv.userPasswordInvalid");
      case "Current password is incorrect": return t("changePassword.currentIncorrect");
      default: return s;
    }
  };

  const doChange = async () => {
    setError("");
    if (requireCurrent && !currentPw) { setError(t("changePassword.currentRequired")); return; }
    const pwKey = passwordValidationKey(newPw);
    if (pwKey) { setError(t(pwKey)); return; }
    if (newPw !== confirmPw) { setError(t("changePassword.mismatch")); return; }
    setIsBusy(true);
    try {
      await safeFetch(`${apiBase}/api/auth/change-password`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_password: currentPw, new_password: newPw }),
      });
      toast.success(t("changePassword.changed"));
      // token_version was bumped — every session is now invalid, force re-login.
      onDone();
    } catch (e) {
      setError(localizedError(e instanceof Error ? e.message : e));
    } finally {
      setIsBusy(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>{t("changePassword.title")}</DialogTitle>
          <DialogDescription>{t("changePassword.description")}</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="cp-current">{t("changePassword.current")}</Label>
            <Input
              id="cp-current"
              type="password"
              autoComplete="current-password"
              value={currentPw}
              onChange={(e) => setCurrentPw(e.target.value)}
              placeholder={t("changePassword.currentPlaceholder")}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="cp-new">{t("changePassword.new")}</Label>
            <Input
              id="cp-new"
              type="password"
              autoComplete="new-password"
              value={newPw}
              onChange={(e) => setNewPw(e.target.value)}
              placeholder={t("changePassword.newPlaceholder")}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="cp-confirm">{t("changePassword.confirm")}</Label>
            <Input
              id="cp-confirm"
              type="password"
              autoComplete="new-password"
              value={confirmPw}
              onChange={(e) => setConfirmPw(e.target.value)}
              placeholder={t("changePassword.confirmPlaceholder")}
              onKeyDown={(e) => { if (e.key === "Enter" && !isBusy) void doChange(); }}
            />
          </div>
          {error && <p className="text-xs text-red-500">{error}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <Button variant="outline" size="sm" onClick={onClose} disabled={isBusy}>
              {t("common.cancel", { defaultValue: "取消" })}
            </Button>
            <Button size="sm" onClick={() => { void doChange(); }} disabled={isBusy || !newPw || !confirmPw}>
              {t("changePassword.submit")}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
