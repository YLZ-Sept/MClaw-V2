import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { DatabaseZap, Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { DotGray, DotGreen, IconChevronUp, IconCircle, IconEdit, IconPower, IconRefresh, IconTrash } from "../icons";
import { safeFetch } from "../providers";
import type { EndpointDraft, EnvMap } from "../types";
import { notifyError, notifyLoading, notifySuccess, dismissLoading } from "../utils/notify";

type RerankStyle = "auto" | "dashscope" | "openai_rerank" | "siliconflow";

type KnowledgeEndpointForm = {
  name: string;
  provider: string;
  baseUrl: string;
  apiKey: string;
  apiKeyEnv: string;
  model: string;
  style: RerankStyle;
  timeout: number;
};

type Preset = {
  provider: string;
  baseUrl: string;
  model: string;
  style: RerankStyle;
};

const PRESETS: Record<string, Preset> = {
  bailian_qwen37: {
    provider: "dashscope",
    baseUrl: "",
    model: "qwen3.7-text-rerank",
    style: "dashscope",
  },
  bailian_qwen3: {
    provider: "dashscope",
    baseUrl: "",
    model: "qwen3-rerank",
    style: "openai_rerank",
  },
  zhipu: {
    provider: "zhipu",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    model: "rerank",
    style: "siliconflow",
  },
  siliconflow: {
    provider: "siliconflow",
    baseUrl: "https://api.siliconflow.cn/v1",
    model: "BAAI/bge-reranker-v2-m3",
    style: "siliconflow",
  },
  jina: {
    provider: "jina",
    baseUrl: "https://api.jina.ai/v1",
    model: "jina-reranker-v3.5",
    style: "siliconflow",
  },
  cohere: {
    provider: "cohere",
    baseUrl: "https://api.cohere.com/v2",
    model: "rerank-v4.0-fast",
    style: "siliconflow",
  },
  custom: {
    provider: "custom",
    baseUrl: "",
    model: "",
    style: "auto",
  },
};

function emptyForm(): KnowledgeEndpointForm {
  return {
    name: "",
    provider: PRESETS.bailian_qwen37.provider,
    baseUrl: "",
    apiKey: "",
    apiKeyEnv: "",
    model: PRESETS.bailian_qwen37.model,
    style: PRESETS.bailian_qwen37.style,
    timeout: 5,
  };
}

export interface KnowledgeEndpointsSectionProps {
  endpoints: EndpointDraft[];
  envDraft: EnvMap;
  disabled: boolean;
  disabledMessage: string;
  httpApiBase: () => string;
  reloadEndpoints: () => Promise<void>;
  askConfirm: (message: string, onConfirm: () => void) => void;
}

export function KnowledgeEndpointsSection({
  endpoints,
  envDraft,
  disabled,
  disabledMessage,
  httpApiBase,
  reloadEndpoints,
  askConfirm,
}: KnowledgeEndpointsSectionProps) {
  const { t } = useTranslation();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [originalName, setOriginalName] = useState<string | null>(null);
  const [form, setForm] = useState<KnowledgeEndpointForm>(emptyForm);
  const [presetKey, setPresetKey] = useState("bailian_qwen37");

  const nextPriority = useMemo(
    () => endpoints.reduce((max, endpoint) => Math.max(max, endpoint.priority || 0), 0) + 10,
    [endpoints],
  );

  function startAdd() {
    setOriginalName(null);
    setPresetKey("bailian_qwen37");
    setForm(emptyForm());
    setDialogOpen(true);
  }

  function startEdit(endpoint: EndpointDraft) {
    const extra = (endpoint.extra_params || {}) as Record<string, unknown>;
    const rawStyle = String(extra.style || "auto");
    const style: RerankStyle = (["auto", "dashscope", "openai_rerank", "siliconflow"] as RerankStyle[]).includes(
      rawStyle as RerankStyle,
    )
      ? (rawStyle as RerankStyle)
      : "auto";
    setOriginalName(endpoint.name);
    setPresetKey("custom");
    setForm({
      name: endpoint.name,
      provider: endpoint.provider,
      baseUrl: endpoint.base_url,
      apiKey: "",
      apiKeyEnv: endpoint.api_key_env,
      model: endpoint.model,
      style,
      timeout: endpoint.timeout || 5,
    });
    setDialogOpen(true);
  }

  function applyPreset(key: string) {
    const preset = PRESETS[key] || PRESETS.custom;
    setPresetKey(key);
    setForm((current) => ({
      ...current,
      provider: preset.provider,
      baseUrl: preset.baseUrl || current.baseUrl,
      model: preset.model || current.model,
      style: preset.style,
    }));
  }

  async function save() {
    if (!form.name.trim() || !form.baseUrl.trim() || !form.model.trim()) {
      notifyError(t("llm.knowledgeRequired"));
      return;
    }
    if (!/^https?:\/\//i.test(form.baseUrl.trim())) {
      notifyError(t("llm.knowledgeBaseUrlInvalid"));
      return;
    }
    if (!originalName && !form.apiKey.trim()) {
      notifyError(t("llm.knowledgeApiKeyRequired"));
      return;
    }

    const busyId = notifyLoading(t("llm.knowledgeSaving"));
    try {
      const endpoint = {
        name: form.name.trim(),
        provider: form.provider,
        api_type: "rerank",
        base_url: form.baseUrl.trim(),
        api_key_env: form.apiKeyEnv,
        model: form.model.trim(),
        priority: originalName
          ? endpoints.find((item) => item.name === originalName)?.priority || nextPriority
          : nextPriority,
        timeout: Math.max(1, Number(form.timeout) || 5),
        capabilities: ["rerank"],
        extra_params: { style: form.style },
      };
      const response = await safeFetch(`${httpApiBase()}/api/config/save-endpoint`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          endpoint,
          api_key: form.apiKey.trim() || null,
          endpoint_type: "knowledge_endpoints",
          original_name: originalName,
        }),
      });
      const data = await response.json();
      if (data.status !== "ok") throw new Error(data.error || t("llm.knowledgeSaveFailed"));
      await reloadEndpoints();
      setDialogOpen(false);
      notifySuccess(t("llm.knowledgeSaved"));
    } catch (error) {
      notifyError(String((error as Error)?.message || error));
    } finally {
      dismissLoading(busyId);
    }
  }

  async function mutate(path: string, init: RequestInit) {
    try {
      const response = await safeFetch(`${httpApiBase()}${path}`, init);
      const data = await response.json();
      if (data.status !== "ok") throw new Error(data.error || t("llm.knowledgeSaveFailed"));
      await reloadEndpoints();
    } catch (error) {
      notifyError(String((error as Error)?.message || error));
    }
  }

  async function testEndpoint(endpoint: EndpointDraft) {
    const busyId = notifyLoading(t("llm.knowledgeTesting"));
    try {
      const response = await safeFetch(`${httpApiBase()}/api/config/test-endpoint`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint_type: "knowledge_endpoints", name: endpoint.name }),
      });
      const data = await response.json();
      if (data.status !== "ok") throw new Error(data.error || t("llm.knowledgeTestFailed"));
      notifySuccess(t("llm.knowledgeTestOk", { ms: Number(data.latency_ms || 0) }));
    } catch (error) {
      notifyError(String((error as Error)?.message || error));
    } finally {
      dismissLoading(busyId);
    }
  }

  function moveUp(name: string) {
    const names = endpoints.map((endpoint) => endpoint.name);
    const index = names.indexOf(name);
    if (index <= 0) return;
    [names[index - 1], names[index]] = [names[index], names[index - 1]];
    void mutate("/api/config/reorder-endpoints", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ordered_names: names, endpoint_type: "knowledge_endpoints" }),
    });
  }

  function styleLabel(style: string) {
    const key = `llm.knowledgeStyle_${style || "auto"}`;
    const translated = t(key);
    return translated === key ? style : translated;
  }

  return (
    <>
      <div className="card" style={{ marginTop: 10 }}>
        <div className="mb-2 flex items-start justify-between gap-3">
          <div>
            <div className="cardTitle">{t("llm.knowledgeEndpoints")}</div>
            <div className="cardHint">{t("llm.knowledgeEndpointsHint")}</div>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="bg-primary/5 border-primary/30 text-primary hover:bg-primary/10 hover:text-primary"
            onClick={startAdd}
            disabled={disabled}
            title={disabled ? disabledMessage : undefined}
          >
            <Plus size={15} />
            {t("llm.addKnowledgeEndpoint")}
          </Button>
        </div>

        {endpoints.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-7 text-muted-foreground">
            <DatabaseZap size={28} strokeWidth={1.5} className="mb-2 opacity-35" />
            <p className="text-sm">{t("llm.knowledgeNoEndpoints")}</p>
            <p className="mt-1 text-xs opacity-70">{t("llm.knowledgeEnvFallback")}</p>
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="w-[34px]"></TableHead>
                <TableHead>{t("status.endpoint")}</TableHead>
                <TableHead>{t("llm.knowledgeStyle")}</TableHead>
                <TableHead>{t("status.model")}</TableHead>
                <TableHead className="w-[140px]"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {endpoints.map((endpoint, index) => (
                <TableRow
                  key={endpoint.name}
                  className={cn(endpoint.enabled === false ? "opacity-45" : undefined)}
                >
                  <TableCell className="align-middle">
                    {(envDraft[endpoint.api_key_env] || "").trim() ? <DotGreen /> : <DotGray />}
                  </TableCell>
                  <TableCell className="font-semibold">
                    <span>{endpoint.name}</span>
                    {index === 0 && endpoint.enabled !== false && (
                      <span className="ml-1.5 text-[10px] font-extrabold text-primary">{t("llm.primary")}</span>
                    )}
                    {endpoint.enabled === false && (
                      <span className="ml-1.5 text-[10px] font-bold text-muted-foreground">{t("llm.disabled")}</span>
                    )}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {styleLabel(String(((endpoint.extra_params || {}) as Record<string, unknown>).style || endpoint.api_type || "auto"))}
                  </TableCell>
                  <TableCell className="text-muted-foreground">{endpoint.model}</TableCell>
                  <TableCell>
                    <div className="flex gap-1 justify-end">
                      <Button variant="ghost" size="icon-sm" className="text-muted-foreground hover:text-foreground" style={index === 0 ? { visibility: "hidden" } : undefined} onClick={() => moveUp(endpoint.name)} disabled={disabled} title={disabled ? disabledMessage : t("llm.moveUp")}><IconChevronUp size={14} /></Button>
                      <Button variant="ghost" size="icon-sm" className="text-muted-foreground hover:text-foreground" onClick={() => void testEndpoint(endpoint)} disabled={disabled} title={disabled ? disabledMessage : t("llm.knowledgeTest")}><IconRefresh size={14} /></Button>
                      <Button variant="ghost" size="icon-sm" className="text-muted-foreground hover:text-foreground" onClick={() => void mutate("/api/config/toggle-endpoint", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: endpoint.name, endpoint_type: "knowledge_endpoints" }) })} disabled={disabled} title={disabled ? disabledMessage : endpoint.enabled === false ? t("llm.enable") : t("llm.disable")}>{endpoint.enabled !== false ? <IconPower size={14} /> : <IconCircle size={14} />}</Button>
                      <Button variant="ghost" size="icon-sm" className="text-muted-foreground hover:text-foreground" onClick={() => startEdit(endpoint)} disabled={disabled} title={disabled ? disabledMessage : t("llm.edit")}><IconEdit size={14} /></Button>
                      <Button variant="ghost" size="icon-sm" className="text-muted-foreground hover:text-destructive hover:bg-destructive/10" onClick={() => askConfirm(`${t("common.confirmDeleteMsg")} "${endpoint.name}"?`, () => void mutate(`/api/config/endpoint/${encodeURIComponent(endpoint.name)}?endpoint_type=knowledge_endpoints`, { method: "DELETE" }))} disabled={disabled} title={disabled ? disabledMessage : t("common.delete")}><IconTrash size={14} /></Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-[520px]">
          <DialogHeader>
            <DialogTitle>{originalName ? t("llm.editKnowledgeEndpoint") : t("llm.addKnowledgeEndpoint")}</DialogTitle>
            <DialogDescription className="sr-only">{t("llm.knowledgeEndpointsHint")}</DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-2">
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>{t("llm.knowledgeProvider")}</Label>
                <Select value={presetKey} onValueChange={applyPreset} disabled={!!originalName}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="bailian_qwen37">{t("llm.knowledgePreset_bailian_qwen37")}</SelectItem>
                    <SelectItem value="bailian_qwen3">{t("llm.knowledgePreset_bailian_qwen3")}</SelectItem>
                    <SelectItem value="zhipu">{t("llm.knowledgePreset_zhipu")}</SelectItem>
                    <SelectItem value="siliconflow">{t("llm.knowledgePreset_siliconflow")}</SelectItem>
                    <SelectItem value="jina">{t("llm.knowledgePreset_jina")}</SelectItem>
                    <SelectItem value="cohere">{t("llm.knowledgePreset_cohere")}</SelectItem>
                    <SelectItem value="custom">{t("llm.customProvider")}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>{t("llm.knowledgeStyle")}</Label>
                <Select value={form.style} onValueChange={(value) => setForm((current) => ({ ...current, style: value as RerankStyle }))}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="auto">{t("llm.knowledgeStyle_auto")}</SelectItem>
                    <SelectItem value="dashscope">{t("llm.knowledgeStyle_dashscope")}</SelectItem>
                    <SelectItem value="openai_rerank">{t("llm.knowledgeStyle_openai_rerank")}</SelectItem>
                    <SelectItem value="siliconflow">{t("llm.knowledgeStyle_siliconflow")}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>{t("llm.endpointName")}</Label>
              <Input value={form.name} onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))} placeholder="bailian-rerank" />
            </div>
            <div className="space-y-1.5">
              <Label>{t("llm.baseUrl")}</Label>
              <Input value={form.baseUrl} onChange={(event) => setForm((current) => ({ ...current, baseUrl: event.target.value }))} placeholder="https://.../api/v1/services/rerank/text-rerank/text-rerank" />
            </div>
            <div className="space-y-1.5">
              <Label>{t("llm.selectModel")}</Label>
              <Input value={form.model} onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))} placeholder="qwen3.7-text-rerank" />
            </div>
            <div className="space-y-1.5">
              <Label>{t("llm.knowledgeApiKey")}</Label>
              <Input type="password" value={form.apiKey} onChange={(event) => setForm((current) => ({ ...current, apiKey: event.target.value }))} placeholder={originalName ? t("llm.knowledgeKeepApiKey") : "sk-..."} />
            </div>
            <div className="space-y-1.5">
              <Label>{t("llm.advTimeout")}</Label>
              <Input type="number" min={1} value={form.timeout} onChange={(event) => setForm((current) => ({ ...current, timeout: Number(event.target.value) }))} />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>{t("common.cancel")}</Button>
            <Button onClick={() => void save()}>{t("common.save")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
