import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { safeFetch } from "../providers";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel,
  AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table, TableHeader, TableBody, TableHead, TableRow, TableCell,
} from "@/components/ui/table";
import {
  Loader2, RefreshCw, Trash2, Search, Plus, Upload, Globe,
  BookOpen, FileText, Database, Pencil, FolderOpen,
} from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────────────

type KBCollection = {
  id: string;
  name: string;
  description: string;
  created_at: number;
  updated_at: number;
  doc_count: number;
  chunk_count: number;
};

type KBDocument = {
  id: string;
  collection_id: string;
  filename: string;
  file_path: string | null;
  file_type: string;
  file_size: number;
  checksum: string;
  status: string;
  chunk_count: number;
  error_message: string | null;
  created_at: number;
  updated_at: number;
};

type SearchResult = {
  chunk: {
    id: string;
    doc_id: string;
    collection_id: string;
    content: string;
    chunk_index: number;
    token_count: number;
  };
  score: number;
  bm25_score: number;
  vector_score: number;
  source_type: string;
};

type Stats = {
  collection_id: string;
  doc_count: number;
  chunk_count: number;
  total_chars: number;
  storage_bytes: number;
  indexed_count: number;
  error_count: number;
};

// ── Props ─────────────────────────────────────────────────────────────────────

type Props = {
  serviceRunning: boolean;
  apiBaseUrl: string;
};

export function KnowledgeBaseView({ serviceRunning, apiBaseUrl }: Props) {
  const { t } = useTranslation();

  // State
  const [collections, setCollections] = useState<KBCollection[]>([]);
  const [selectedColl, setSelectedColl] = useState<KBCollection | null>(null);
  const [documents, setDocuments] = useState<KBDocument[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(true);

  // Create collection dialog
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");

  // Delete confirm
  const [confirmMsg, setConfirmMsg] = useState("");
  const [confirmCb, setConfirmCb] = useState<(() => void) | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [editingColl, setEditingColl] = useState<KBCollection | null>(null);
  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");

  // Import URL
  const [showImportUrl, setShowImportUrl] = useState(false);
  const [importUrl, setImportUrl] = useState("");

  // Search
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);

  const API = apiBaseUrl;

  // ── Fetch collections ────────────────────────────────────────────────────

  const loadCollections = useCallback(async () => {
    try {
      const res = await safeFetch(`${API}/api/knowledge/collections`);
      const data = res.ok ? await res.json() : [];
      setCollections(Array.isArray(data) ? data : []);
    } catch {
      // silent for non-running backend
    }
  }, [API]);

  const loadDocuments = useCallback(async (collId: string) => {
    try {
      const [docsRes, statsRes] = await Promise.all([
        safeFetch(`${API}/api/knowledge/collections/${collId}/documents`),
        safeFetch(`${API}/api/knowledge/collections/${collId}/stats`),
      ]);
      if (docsRes.ok) setDocuments(await docsRes.json());
      if (statsRes.ok) setStats(await statsRes.json());
    } catch {
      // silent
    }
  }, []);

  useEffect(() => {
    if (!serviceRunning) { setLoading(false); return; }
    setLoading(true);
    loadCollections().finally(() => setLoading(false));
  }, [serviceRunning, loadCollections]);

  useEffect(() => {
    if (selectedColl) {
      loadDocuments(selectedColl.id);
    }
  }, [selectedColl, loadDocuments]);

  // ── Actions ──────────────────────────────────────────────────────────────

  const doCreateCollection = async () => {
    if (!newName.trim()) return;
    try {
      const res = await safeFetch(`${API}/api/knowledge/collections`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName.trim(), description: newDesc.trim() }),
      });
      if (res.ok) {
        toast.success(t("knowledge.createCollection") + " ✓");
        setShowCreate(false);
        setNewName("");
        setNewDesc("");
        await loadCollections();
      }
    } catch {
      toast.error(t("common.failed"));
    }
  };

  const doDeleteCollection = async (collId: string) => {
    try {
      const res = await safeFetch(`${API}/api/knowledge/collections/${collId}`, { method: "DELETE" });
      if (res.ok) {
        toast.success(t("knowledge.deleted"));
        if (selectedColl?.id === collId) setSelectedColl(null);
        await loadCollections();
      }
    } catch {
      toast.error(t("common.failed"));
    }
  };

  const doEditCollection = async () => {
    if (!editingColl || !editName.trim()) return;
    try {
      const res = await safeFetch(`${apiBaseUrl}/api/knowledge/collections/${editingColl.id}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: editName.trim(), description: editDesc.trim() }),
      });
      if (res.ok) { setEditingColl(null); await loadCollections(); }
    } catch { toast.error(t("common.failed")); }
  };

  /** 递归遍历拖放的目录条目，收集所有文件 */
  const collectFilesFromEntries = async (entries: any[]): Promise<File[]> => {
    const files: File[] = [];
    for (const entry of entries) {
      if (entry.isFile) {
        const file: File = await new Promise((resolve) => entry.file(resolve));
        files.push(file);
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        const readAllEntries = (): Promise<any[]> =>
          new Promise((resolve) => {
            const all: any[] = [];
            const read = () => {
              reader.readEntries((batch: any[]) => {
                if (batch.length) { all.push(...batch); read(); }
                else resolve(all);
              });
            };
            read();
          });
        const subEntries = await readAllEntries();
        const subFiles = await collectFilesFromEntries(subEntries);
        files.push(...subFiles);
      }
    }
    return files;
  };

  const uploadFileList = async (files: File[]) => {
    if (!files.length || !selectedColl) return;
    const token = localStorage.getItem("mclaw_access_token");
    for (const file of files) {
      const formData = new FormData();
      formData.append("file", file);
      try {
        const headers: Record<string, string> = {};
        if (token) headers["Authorization"] = `Bearer ${token}`;
        const res = await fetch(`${API}/api/knowledge/collections/${selectedColl.id}/upload`, {
          method: "POST", body: formData, headers,
        });
        if (res.ok) {
          toast.success(`${file.name} ✓`);
        } else {
          const err = await res.json();
          toast.error(err.detail || t("knowledge.uploadFailed", { name: file.name }));
        }
      } catch {
        toast.error(t("knowledge.uploadFailed", { name: file.name }));
      }
    }
    await loadDocuments(selectedColl.id);
    await loadCollections();
  };

  const doUploadFiles = async (files: FileList | null) => {
    if (!files || !selectedColl) return;
    await uploadFileList(Array.from(files));
  };

  const doImportUrl = async () => {
    if (!importUrl.trim() || !selectedColl) return;
    try {
      const res = await safeFetch(
        `${API}/api/knowledge/collections/${selectedColl.id}/import-url`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: importUrl.trim() }),
          signal: AbortSignal.timeout(60_000),
        },
      );
      if (res.ok) {
        toast.success(t("knowledge.import") + " ✓");
        setShowImportUrl(false);
        setImportUrl("");
        await loadDocuments(selectedColl.id);
        await loadCollections();
      } else {
        const err = await res.json();
        toast.error(err.detail || "Failed");
      }
    } catch {
      toast.error(t("common.failed"));
    }
  };

  const doDeleteDoc = async (docId: string) => {
    try {
      const res = await safeFetch(`${API}/api/knowledge/documents/${docId}`, { method: "DELETE" });
      if (res.ok) {
        toast.success(t("knowledge.deleted"));
        await loadDocuments(selectedColl!.id);
        await loadCollections();
      }
    } catch {
      toast.error(t("common.failed"));
    }
  };

  const doReindex = async (docId: string) => {
    try {
      const res = await safeFetch(`${API}/api/knowledge/documents/${docId}/reindex`,
        { method: "POST", signal: AbortSignal.timeout(120_000) });
      if (res.ok) {
        toast.success(t("knowledge.reindexOk"));
        await loadDocuments(selectedColl!.id);
      }
    } catch {
      toast.error(t("common.failed"));
    }
  };

  const doSearch = async () => {
    if (!searchQuery.trim()) return;
    setSearching(true);
    try {
      const res = await safeFetch(`${API}/api/knowledge/search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: searchQuery.trim(),
          collection_id: selectedColl?.id || undefined,
          top_k: 20,
        }),
      });
      if (res.ok) {
        setSearchResults(await res.json());
      }
    } catch {
      toast.error("Search failed");
    }
    setSearching(false);
  };

  // ── Helpers ──────────────────────────────────────────────────────────────

  const formatSize = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  };

  const formatDate = (ts: number): string => {
    if (!ts) return "-";
    return new Date(ts * 1000).toLocaleDateString();
  };

  const statusBadge = (status: string) => {
    const map: Record<string, { cls: string; label: string }> = {
      pending: { cls: "bg-yellow-100 text-yellow-800", label: t("knowledge.statusPending") },
      indexing: { cls: "bg-blue-100 text-blue-800", label: t("knowledge.statusIndexing") },
      ready: { cls: "bg-green-100 text-green-800", label: t("knowledge.statusReady") },
      error: { cls: "bg-red-100 text-red-800", label: t("knowledge.statusError") },
    };
    const m = map[status] || map.pending;
    return <Badge className={m.cls}>{m.label}</Badge>;
  };

  const sourceBadge = (sourceType: string) => {
    const map: Record<string, string> = {
      bm25: t("knowledge.sourceBm25"),
      vector: t("knowledge.sourceVector"),
      hybrid: t("knowledge.sourceHybrid"),
    };
    return map[sourceType] || sourceType;
  };

  const fileTypeLabel = (ft: string): string => {
    const map: Record<string, string> = {
      pdf: t("knowledge.fileTypePDF"),
      md: t("knowledge.fileTypeMarkdown"),
      txt: t("knowledge.fileTypeText"),
      docx: t("knowledge.fileTypeWord"),
      html: t("knowledge.fileTypeHTML"),
      xlsx: t("knowledge.fileTypeExcel"),
      xls: t("knowledge.fileTypeExcel"),
      csv: t("knowledge.fileTypeCSV"),
      url: t("knowledge.fileTypeURL"),
    };
    return map[ft] || ft;
  };

  // ── Render ───────────────────────────────────────────────────────────────

  if (!serviceRunning) {
    return (
      <div className="viewRoot">
        <div className="text-center py-20 text-muted-foreground">
          <Database size={48} className="mx-auto mb-4 opacity-30" />
          <p>{t("knowledge.serviceNotRunning")}</p>
        </div>
      </div>
    );
  }

  const isMobile = typeof window !== "undefined" && window.innerWidth <= 768;

  return (
    <div className="viewRoot">
      {/* Header */}
      <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
        <h2 className="text-xl font-bold flex items-center gap-2">
          <BookOpen size={22} /> {t("knowledge.title")}
        </h2>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => { loadCollections(); if (selectedColl) loadDocuments(selectedColl.id); }}>
            <RefreshCw size={14} className="mr-1" /> {t("knowledge.search")}
          </Button>
          <Button size="sm" onClick={() => setShowCreate(true)}>
            <Plus size={14} className="mr-1" /> {t("knowledge.createCollection")}
          </Button>
        </div>
      </div>

      {/* Stats Cards */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
          <Card><CardContent className="p-3 text-center">
            <div className="text-lg font-bold">{stats.doc_count}</div>
            <div className="text-xs text-muted-foreground">{t("knowledge.totalDocs")}</div>
          </CardContent></Card>
          <Card><CardContent className="p-3 text-center">
            <div className="text-lg font-bold">{stats.chunk_count}</div>
            <div className="text-xs text-muted-foreground">{t("knowledge.totalChunks")}</div>
          </CardContent></Card>
          <Card><CardContent className="p-3 text-center">
            <div className="text-lg font-bold">{stats.total_chars.toLocaleString()}</div>
            <div className="text-xs text-muted-foreground">{t("knowledge.totalChars")}</div>
          </CardContent></Card>
          <Card><CardContent className="p-3 text-center">
            <div className="text-lg font-bold">{formatSize(stats.storage_bytes)}</div>
            <div className="text-xs text-muted-foreground">{t("knowledge.storageSize")}</div>
          </CardContent></Card>
          <Card><CardContent className="p-3 text-center">
            <div className="text-lg font-bold">{stats.indexed_count}</div>
            <div className="text-xs text-muted-foreground">Indexed</div>
          </CardContent></Card>
        </div>
      )}

      <div className={`flex ${isMobile ? "flex-col" : "flex-row"} gap-4`}>
        {/* Left: Collections list */}
        <div className={isMobile ? "w-full" : "w-64 shrink-0"}>
          <Card>
            <CardContent className="p-3">
              <h3 className="font-semibold text-sm mb-2">{t("knowledge.collections")}</h3>
              <div
                className={`cursor-pointer px-2 py-1.5 rounded text-sm ${!selectedColl ? "bg-accent font-bold" : "hover:bg-accent/50"}`}
                onClick={() => setSelectedColl(null)}
              >
                {t("knowledge.allCollections")}
              </div>
              {loading && (
                <div className="flex justify-center py-4"><Loader2 size={16} className="animate-spin" /></div>
              )}
              {collections.map((c) => (
                <div
                  key={c.id}
                  className={`cursor-pointer px-2 py-1.5 rounded text-sm flex items-center justify-between group ${selectedColl?.id === c.id ? "bg-accent font-bold" : "hover:bg-accent/50"}`}
                  onClick={() => setSelectedColl(c)}
                >
                  <span className="truncate">{c.name}</span>
                  <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100">
                    <span className="text-xs text-muted-foreground">{c.doc_count}</span>
                    <Pencil
                      size={12}
                      className="text-muted-foreground hover:text-foreground"
                      onClick={(e) => { e.stopPropagation(); setEditingColl(c); setEditName(c.name); setEditDesc(c.description); }}
                    />
                    <Trash2
                      size={12}
                      className="text-red-400 hover:text-red-600"
                      onClick={(e) => { e.stopPropagation(); setConfirmMsg(t("knowledge.confirmDelete") + ` "${c.name}"`); setConfirmCb(() => () => doDeleteCollection(c.id)); }}
                    />
                  </div>
                </div>
              ))}
              {!loading && collections.length === 0 && (
                <p className="text-xs text-muted-foreground py-4 text-center">{t("knowledge.noCollections")}</p>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Right: Documents + Search (full-panel drop zone) */}
        <div
          className="flex-1 min-w-0 space-y-4 relative"
          onDragOver={(e) => { e.preventDefault(); if (selectedColl) setDragOver(true); }}
          onDragLeave={(e) => { if (e.currentTarget === e.target) setDragOver(false); }}
          onDrop={async (e) => {
            e.preventDefault(); setDragOver(false);
            if (!selectedColl || !e.dataTransfer.items.length) return;
            // 支持文件夹拖放
            const entries: any[] = [];
            for (let i = 0; i < e.dataTransfer.items.length; i++) {
              const entry = e.dataTransfer.items[i].webkitGetAsEntry?.();
              if (entry) entries.push(entry);
            }
            if (entries.length) {
              const files = await collectFilesFromEntries(entries);
              if (files.length) await uploadFileList(files);
            } else if (e.dataTransfer.files.length) {
              doUploadFiles(e.dataTransfer.files);
            }
          }}
        >
          {/* Toolbar */}
          {selectedColl && (
            <div className="flex items-center gap-2 flex-wrap">
              <Button size="sm" variant="outline" onClick={() => document.getElementById("kb-file-input")?.click()}>
                <Upload size={14} className="mr-1" /> {t("knowledge.uploadDocument")}
              </Button>
              <input
                id="kb-file-input"
                type="file"
                multiple
                accept=".pdf,.md,.txt,.doc,.docx,.html,.htm,.xlsx,.xls,.csv"
                className="hidden"
                onChange={(e) => doUploadFiles(e.target.files)}
              />
              <Button size="sm" variant="outline" onClick={() => document.getElementById("kb-folder-input")?.click()}>
                <FolderOpen size={14} className="mr-1" /> {t("knowledge.uploadFolder")}
              </Button>
              <input
                id="kb-folder-input"
                type="file"
                /* @ts-ignore */
                webkitdirectory=""
                /* @ts-ignore */
                directory=""
                className="hidden"
                onChange={(e) => doUploadFiles(e.target.files)}
              />
              <Button size="sm" variant="outline" onClick={() => setShowImportUrl(true)}>
                <Globe size={14} className="mr-1" /> {t("knowledge.importUrl")}
              </Button>
              <span className="text-xs text-muted-foreground">{t("knowledge.dragDropHint")}</span>
            </div>
          )}

          {/* Search bar */}
          <div className="flex items-center gap-2">
            <Input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder={t("knowledge.searchPlaceholder")}
              className="max-w-md"
              onKeyDown={(e) => e.key === "Enter" && doSearch()}
            />
            <Button size="sm" onClick={doSearch} disabled={searching}>
              {searching ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />}
            </Button>
          </div>

          {/* Search results */}
          {searchResults.length > 0 && (
            <Card>
              <CardContent className="p-3">
                <h3 className="font-semibold text-sm mb-2">{t("knowledge.searchResult")} ({searchResults.length})</h3>
                <div className="space-y-2 max-h-96 overflow-auto">
                  {searchResults.map((r, i) => (
                    <div key={i} className="border rounded p-2 text-sm">
                      <div className="flex items-center gap-2 mb-1">
                        <Badge variant="outline" className="text-xs">{sourceBadge(r.source_type)}</Badge>
                        <span className="text-xs text-muted-foreground">{t("knowledge.score")}: {r.score.toFixed(4)}</span>
                      </div>
                      <p className="text-xs whitespace-pre-wrap line-clamp-6">{r.chunk.content}</p>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
          )}

          {/* Search no results */}
          {searchResults.length === 0 && searchQuery && !searching && (
            <p className="text-sm text-muted-foreground">{t("knowledge.noResult")}</p>
          )}

          {/* Document list */}
          {selectedColl && (
            <Card>
              <CardContent className="p-3">
                <h3 className="font-semibold text-sm mb-2">{t("knowledge.documents")} ({documents.length})</h3>
                {documents.length === 0 ? (
                  <p className="text-sm text-muted-foreground py-4 text-center">{t("knowledge.noDocuments")}</p>
                ) : isMobile ? (
                  /* Mobile: Card layout */
                  <div className="space-y-2">
                    {documents.map((doc) => (
                      <Card key={doc.id} className="p-3">
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-2 min-w-0">
                            <FileText size={16} className="text-muted-foreground shrink-0" />
                            <span className="text-sm truncate">{doc.filename}</span>
                          </div>
                          <div className="flex items-center gap-1 shrink-0">
                            {statusBadge(doc.status)}
                            <Trash2 size={14} className="text-red-400 cursor-pointer" onClick={() => { setConfirmMsg(`${t("knowledge.deleteDocument")}: ${doc.filename}`); setConfirmCb(() => () => doDeleteDoc(doc.id)); }} />
                          </div>
                        </div>
                        <div className="flex gap-2 mt-1 text-xs text-muted-foreground">
                          <span>{fileTypeLabel(doc.file_type)}</span>
                          <span>{formatSize(doc.file_size)}</span>
                          <span>{t("knowledge.chunkCount", { count: doc.chunk_count })}</span>
                          <span>{formatDate(doc.updated_at)}</span>
                        </div>
                        {doc.status === "error" && doc.error_message && (
                          <p className="text-xs text-red-500 mt-1">{doc.error_message}</p>
                        )}
                        <Button size="sm" variant="ghost" className="mt-1 h-6 text-xs" onClick={() => doReindex(doc.id)}>
                          <RefreshCw size={12} className="mr-1" /> {t("knowledge.reindex")}
                        </Button>
                      </Card>
                    ))}
                  </div>
                ) : (
                  /* Desktop: Table */
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>{t("knowledge.documents")}</TableHead>
                        <TableHead className="w-16">{t("knowledge.headerType")}</TableHead>
                        <TableHead className="w-16">{t("knowledge.headerSize")}</TableHead>
                        <TableHead className="w-12">{t("knowledge.headerChunks")}</TableHead>
                        <TableHead className="w-12">{t("knowledge.headerStatus")}</TableHead>
                        <TableHead className="w-24">{t("knowledge.headerDate")}</TableHead>
                        <TableHead className="w-16"></TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {documents.map((doc) => (
                        <TableRow key={doc.id}>
                          <TableCell>
                            <div className="flex items-center gap-2">
                              <FileText size={14} className="text-muted-foreground" />
                              <span className="text-sm truncate max-w-[200px]" title={doc.filename}>{doc.filename}</span>
                            </div>
                          </TableCell>
                          <TableCell className="text-xs">{fileTypeLabel(doc.file_type)}</TableCell>
                          <TableCell className="text-xs">{formatSize(doc.file_size)}</TableCell>
                          <TableCell className="text-xs">{doc.chunk_count}</TableCell>
                          <TableCell>{statusBadge(doc.status)}</TableCell>
                          <TableCell className="text-xs text-muted-foreground">{formatDate(doc.updated_at)}</TableCell>
                          <TableCell>
                            <div className="flex items-center gap-1">
                              <Button size="sm" variant="ghost" className="h-7 w-7 p-0" title={t("knowledge.reindex")} onClick={() => doReindex(doc.id)}>
                                <RefreshCw size={12} />
                              </Button>
                              <Button size="sm" variant="ghost" className="h-7 w-7 p-0" title={t("knowledge.deleteDocument")} onClick={() => { setConfirmMsg(`${t("knowledge.deleteDocument")}: ${doc.filename}`); setConfirmCb(() => () => doDeleteDoc(doc.id)); }}>
                                <Trash2 size={12} className="text-red-400" />
                              </Button>
                            </div>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          )}
          {/* Drag-overlay: full-panel drop target */}
          {dragOver && selectedColl && (
            <div className="absolute inset-0 z-40 flex items-center justify-center bg-primary/10 backdrop-blur-[1px] rounded-lg border-2 border-dashed border-primary"
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => { e.preventDefault(); setDragOver(false); if (e.dataTransfer.files.length) doUploadFiles(e.dataTransfer.files); }}
            >
              <div className="text-center pointer-events-none">
                <Upload size={48} className="mx-auto mb-3 text-primary opacity-80" />
                <p className="text-lg font-semibold text-primary">{t("knowledge.dropHere")}</p>
                <p className="text-sm text-muted-foreground mt-1">{t("knowledge.dragDropHint")}</p>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Create Collection Dialog */}
      {showCreate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30" onClick={() => setShowCreate(false)}>
          <Card className="w-[400px] max-w-[90vw]" onClick={(e) => e.stopPropagation()}>
            <CardContent className="p-4">
              <h3 className="font-bold mb-3">{t("knowledge.createCollection")}</h3>
              <Input
                autoFocus
                placeholder={t("knowledge.collectionNamePlaceholder")}
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                className="mb-2"
                onKeyDown={(e) => e.key === "Enter" && doCreateCollection()}
              />
              <Input
                placeholder={t("knowledge.collectionDescPlaceholder")}
                value={newDesc}
                onChange={(e) => setNewDesc(e.target.value)}
                className="mb-3"
              />
              <div className="flex justify-end gap-2">
                <Button variant="outline" size="sm" onClick={() => setShowCreate(false)}>{t("common.cancel")}</Button>
                <Button size="sm" onClick={doCreateCollection} disabled={!newName.trim()}>{t("common.create")}</Button>
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {/* Import URL Dialog */}
      {showImportUrl && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30" onClick={() => setShowImportUrl(false)}>
          <Card className="w-[400px] max-w-[90vw]" onClick={(e) => e.stopPropagation()}>
            <CardContent className="p-4">
              <h3 className="font-bold mb-3">{t("knowledge.importUrl")}</h3>
              <Input
                autoFocus
                placeholder={t("knowledge.urlPlaceholder")}
                value={importUrl}
                onChange={(e) => setImportUrl(e.target.value)}
                className="mb-3"
                onKeyDown={(e) => e.key === "Enter" && doImportUrl()}
              />
              <div className="flex justify-end gap-2">
                <Button variant="outline" size="sm" onClick={() => setShowImportUrl(false)}>{t("common.cancel")}</Button>
                <Button size="sm" onClick={doImportUrl} disabled={!importUrl.trim()}>{t("knowledge.import")}</Button>
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {/* Edit Collection Dialog */}
      {editingColl && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30" onClick={() => setEditingColl(null)}>
          <Card className="w-[400px] max-w-[90vw]" onClick={(e) => e.stopPropagation()}>
            <CardContent className="p-4">
              <h3 className="font-bold mb-3">{t("knowledge.editCollection")}</h3>
              <Input
                autoFocus
                placeholder={t("knowledge.collectionName")}
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                className="mb-2"
              />
              <Input
                placeholder={t("knowledge.collectionDesc")}
                value={editDesc}
                onChange={(e) => setEditDesc(e.target.value)}
                className="mb-3"
              />
              <div className="flex justify-end gap-2">
                <Button variant="outline" size="sm" onClick={() => setEditingColl(null)}>{t("common.cancel")}</Button>
                <Button size="sm" onClick={doEditCollection} disabled={!editName.trim()}>{t("common.save")}</Button>
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {/* Confirm Dialog */}
      {confirmCb && (
        <AlertDialog open onOpenChange={() => setConfirmCb(null)}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{t("knowledge.confirmDelete")}</AlertDialogTitle>
              <AlertDialogDescription>{confirmMsg}</AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel onClick={() => setConfirmCb(null)}>{t("common.cancel")}</AlertDialogCancel>
              <AlertDialogAction onClick={() => { confirmCb(); setConfirmCb(null); }}>{t("common.delete")}</AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
    </div>
  );
}
