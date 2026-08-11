import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { safeFetch } from "../providers";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel,
  AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Plus, Trash2, Edit3, Users, Bot, Zap,
} from "lucide-react";

interface AgentProfile {
  id: string; name: string; description: string; icon: string; type: string;
}

interface EmployeeAgent {
  profile_id: string; role_label: string; priority: number;
}

interface DigitalEmployee {
  id: string; name: string; description: string; icon: string;
  agents: EmployeeAgent[];
  routing_mode: string;
  created_at: number; updated_at: number;
}

export function DigitalEmployeeView() {
  const { t } = useTranslation();
  const [employees, setEmployees] = useState<DigitalEmployee[]>([]);
  const [profiles, setProfiles] = useState<AgentProfile[]>([]);
  const [editOpen, setEditOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [editing, setEditing] = useState<DigitalEmployee | null>(null);
  const [deleting, setDeleting] = useState<DigitalEmployee | null>(null);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [icon, setIcon] = useState("🤖");
  const [selectedAgents, setSelectedAgents] = useState<EmployeeAgent[]>([]);
  const [saving, setSaving] = useState(false);

  const getApi = () => {
    const base = (window as any).__MCLAW_API_BASE__ || "";
    return base || "http://127.0.0.1:18900";
  };

  const authHeaders = () => {
    const token = localStorage.getItem("mclaw_access_token");
    return token ? { Authorization: `Bearer ${token}` } : {};
  };

  const load = useCallback(async () => {
    const API = getApi();
    try {
      const [empRes, profRes] = await Promise.all([
        safeFetch(`${API}/api/digital-employees`, { headers: authHeaders() }),
        safeFetch(`${API}/api/agents/profiles`, { headers: authHeaders() }),
      ]);
      if (empRes.ok) {
        const data = await empRes.json();
        setEmployees(Array.isArray(data) ? data : []);
      }
      if (profRes.ok) {
        const data = await profRes.json();
        setProfiles(Array.isArray(data) ? data : (data?.profiles ?? data?.items ?? []));
      }
    } catch { /* silent */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  const openCreate = () => {
    setEditing(null);
    setName(""); setDesc(""); setIcon("🤖"); setSelectedAgents([]);
    setEditOpen(true);
  };

  const openEdit = (emp: DigitalEmployee) => {
    setEditing(emp);
    setName(emp.name); setDesc(emp.description); setIcon(emp.icon);
    setSelectedAgents(emp.agents || []);
    setEditOpen(true);
  };

  const save = async () => {
    if (!name.trim()) { toast.error("名称不能为空"); return; }
    setSaving(true);
    const API = getApi();
    const body = { name: name.trim(), description: desc.trim(), icon, agents: selectedAgents };
    try {
      const url = editing
        ? `${API}/api/digital-employees/${editing.id}`
        : `${API}/api/digital-employees`;
      const res = await safeFetch(url, {
        method: editing ? "PUT" : "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        toast.success(editing ? "已更新" : "已创建");
        setEditOpen(false);
        load();
      } else {
        const err = await res.json();
        toast.error(err.detail || "保存失败");
      }
    } catch { toast.error("保存失败"); }
    finally { setSaving(false); }
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    const API = getApi();
    try {
      const res = await safeFetch(`${API}/api/digital-employees/${deleting.id}`, {
        method: "DELETE", headers: authHeaders(),
      });
      if (res.ok) { toast.success("已删除"); load(); }
      else { toast.error("删除失败"); }
    } catch { toast.error("删除失败"); }
    setDeleteOpen(false);
    setDeleting(null);
  };

  const toggleAgent = (profileId: string) => {
    setSelectedAgents(prev => {
      const exists = prev.find(a => a.profile_id === profileId);
      if (exists) return prev.filter(a => a.profile_id !== profileId);
      return [...prev, { profile_id: profileId, role_label: "", priority: prev.length + 1 }];
    });
  };

  const updateAgentLabel = (profileId: string, label: string) => {
    setSelectedAgents(prev => prev.map(a => a.profile_id === profileId ? { ...a, role_label: label } : a));
  };

  const getProfileName = (pid: string) => profiles.find(p => p.id === pid)?.name || pid.slice(0, 8);

  return (
    <div className="flex-1 p-4 md:p-6 space-y-4 overflow-auto">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-bold flex items-center gap-2">
            <Users size={20} /> 数字员工
          </h2>
          <p className="text-xs text-muted-foreground mt-0.5">
            将多个 Agent 组合成一个数字员工，聊天时自动路由到最合适的 Agent
          </p>
        </div>
        <Button size="sm" onClick={openCreate}>
          <Plus size={14} className="mr-1" /> 创建数字员工
        </Button>
      </div>

      {employees.length === 0 ? (
        <Card>
          <CardContent className="p-8 text-center text-muted-foreground">
            <Bot size={40} className="mx-auto mb-3 opacity-30" />
            <p>暂无数字员工</p>
            <p className="text-xs mt-1">点击"创建数字员工"开始</p>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
          {employees.map(emp => (
            <Card key={emp.id} className="hover:shadow-md transition-shadow">
              <CardContent className="p-4">
                <div className="flex items-start justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span className="text-2xl">{emp.icon || "🤖"}</span>
                    <div>
                      <div className="font-semibold text-sm">{emp.name}</div>
                      <div className="text-xs text-muted-foreground line-clamp-1">{emp.description || "—"}</div>
                    </div>
                  </div>
                  <div className="flex gap-1">
                    <Button size="icon" variant="ghost" className="h-7 w-7" onClick={() => openEdit(emp)}>
                      <Edit3 size={12} />
                    </Button>
                    <Button size="icon" variant="ghost" className="h-7 w-7 text-destructive" onClick={() => { setDeleting(emp); setDeleteOpen(true); }}>
                      <Trash2 size={12} />
                    </Button>
                  </div>
                </div>
                <div className="flex flex-wrap gap-1 mt-2">
                  <Badge variant="outline" className="text-xs">
                    <Zap size={10} className="mr-0.5" />{emp.routing_mode === "auto" ? "自动路由" : "手动"}
                  </Badge>
                  {(emp.agents || []).map(a => (
                    <Badge key={a.profile_id} variant="secondary" className="text-xs">
                      {a.role_label || getProfileName(a.profile_id)}
                    </Badge>
                  ))}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Edit Dialog */}
      <AlertDialog open={editOpen} onOpenChange={setEditOpen}>
        <AlertDialogContent className="max-w-lg max-h-[85vh] overflow-auto">
          <AlertDialogHeader>
            <AlertDialogTitle>{editing ? "编辑数字员工" : "创建数字员工"}</AlertDialogTitle>
            <AlertDialogDescription>
              选择要组合的 Agent，聊天时系统会自动路由到最合适的 Agent
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-3 py-2">
            <div>
              <label className="text-xs font-medium">名称 *</label>
              <Input value={name} onChange={e => setName(e.target.value)} placeholder="例如：HR助手" />
            </div>
            <div>
              <label className="text-xs font-medium">描述</label>
              <Input value={desc} onChange={e => setDesc(e.target.value)} placeholder="描述这个数字员工的职责" />
            </div>
            <div>
              <label className="text-xs font-medium mb-1 block">图标</label>
              <div className="flex flex-wrap gap-1 mb-2">
                {["🤖","👩‍💼","👨‍💻","🧑‍🔧","👩‍🔬","🧑‍🏫","👩‍💻","🧑‍⚖️","👨‍🚀","👩‍🎨","🧑‍🌾","👩‍⚕️","🧑‍🚒","👨‍🏭","🧑‍🎓","👩‍🏫","🧑‍💼","👨‍🔧","👩‍🚀","🧑‍🎤","🦾","🧠","💼","📊","📝","🔍","⚙️","💡","🎯","🚀"].map(emoji => (
                  <button
                    key={emoji}
                    type="button"
                    onClick={() => setIcon(emoji)}
                    className={`text-xl p-1 rounded ${icon === emoji ? "bg-primary/20 ring-1 ring-primary" : "hover:bg-muted"}`}
                    title={emoji}
                  >{emoji}</button>
                ))}
              </div>
              <Input value={icon} onChange={e => setIcon(e.target.value)} placeholder="或直接输入 emoji" className="w-32 text-sm" />
            </div>
            <div>
              <label className="text-xs font-medium mb-1 block">
                绑定 Agent ({selectedAgents.length} 个)
              </label>
              <div className="max-h-48 overflow-auto border rounded-md p-2 space-y-1">
                {profiles.map(p => {
                  const bound = selectedAgents.find(a => a.profile_id === p.id);
                  return (
                    <div key={p.id} className="flex items-center gap-2 py-1">
                      <input
                        type="checkbox"
                        checked={!!bound}
                        onChange={() => toggleAgent(p.id)}
                        className="rounded"
                      />
                      <span className="text-sm flex-1">
                        {p.icon} {p.name}
                        <span className="text-xs text-muted-foreground ml-1">({p.type})</span>
                      </span>
                      {bound && (
                        <Input
                          className="w-24 h-7 text-xs"
                          placeholder="角色标签"
                          value={bound.role_label}
                          onChange={e => updateAgentLabel(p.id, e.target.value)}
                        />
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setEditOpen(false)}>取消</AlertDialogCancel>
            <AlertDialogAction onClick={save} disabled={saving}>
              {saving ? "保存中..." : editing ? "保存" : "创建"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Delete Confirm */}
      <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认删除</AlertDialogTitle>
            <AlertDialogDescription>
              确定要删除数字员工「{deleting?.name}」吗？此操作不可撤销。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setDeleteOpen(false)}>取消</AlertDialogCancel>
            <AlertDialogAction onClick={confirmDelete} className="bg-destructive text-destructive-foreground">
              删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
