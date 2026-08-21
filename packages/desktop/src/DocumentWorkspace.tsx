import {
  AlertTriangle,
  BookOpen,
  Check,
  FileText,
  Languages,
  LoaderCircle,
  Minus,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  RefreshCw,
  RotateCw,
  Sparkles,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { PDFDocumentProxy } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { GatewayApi } from "./gateway";
import type {
  DocumentBlog,
  DocumentLayout,
  DocumentManifest,
  DocumentPageLayout,
  DocumentTranslation,
  ProjectPreset,
} from "./types";

type DocumentView = "document" | "blog";

function BlogAssetImage({ api, workspace, src, alt }: {
  api: GatewayApi;
  workspace: string;
  src?: string;
  alt?: string;
}) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const relative = src?.replace(/^\.\//, "") ?? "";
  const supported = relative.startsWith("blog-assets/");
  useEffect(() => {
    if (!supported) return;
    let cancelled = false;
    let created = "";
    void api.documentBlogAsset(workspace, relative.slice("blog-assets/".length)).then((blob) => {
      if (cancelled) return;
      created = URL.createObjectURL(blob);
      setObjectUrl(created);
    }).catch(() => setObjectUrl(null));
    return () => {
      cancelled = true;
      if (created) URL.revokeObjectURL(created);
    };
  }, [api, relative, supported, workspace]);
  if (!supported) return <span className="document-blog-image-unavailable">{alt || "外部图片已拦截"}</span>;
  if (!objectUrl) return <span className="document-blog-image-loading"><LoaderCircle className="spin" />{alt || "正在加载图片"}</span>;
  return <img src={objectUrl} alt={alt ?? ""} />;
}

function BlogPreview({ api, workspace, markdown }: { api: GatewayApi; workspace: string; markdown: string }) {
  const components = useMemo<Components>(() => ({
    img: ({ src, alt }) => <BlogAssetImage api={api} workspace={workspace} src={src} alt={alt} />,
  }), [api, workspace]);
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{markdown}</ReactMarkdown>;
}

interface DocumentWorkspaceProps {
  api: GatewayApi;
  project: ProjectPreset;
  agentWidth: number;
  agentCollapsed: boolean;
  sessionBusy: boolean;
  sessionError: string | null;
  onAgentWidth: (width: number) => void;
  onAgentCollapsed: (collapsed: boolean) => void;
  onDocumentAction: (
    action: "translate" | "generate_blog",
    options: { locale?: string; source?: "original" | "translation" },
  ) => boolean;
}

const TARGET_LANGUAGES = [
  ["zh-CN", "简体中文"],
  ["en", "English"],
  ["ja", "日本語"],
  ["ko", "한국어"],
  ["fr", "Français"],
  ["de", "Deutsch"],
  ["es", "Español"],
] as const;

function isMissing(error: unknown): boolean {
  return error instanceof Error && /\b404\b|not found/i.test(error.message);
}

function sourceLooksChinese(layout: DocumentLayout): boolean {
  const sample = layout.pages.flatMap((page) => page.blocks).map((block) => block.text).join("").slice(0, 5000);
  const letters = sample.match(/[\p{L}]/gu)?.length ?? 0;
  const chinese = sample.match(/[\p{Script=Han}]/gu)?.length ?? 0;
  return letters > 0 && chinese / letters > 0.35;
}

async function loadPdf(data: ArrayBuffer): Promise<PDFDocumentProxy> {
  const pdfjs = await import("pdfjs-dist");
  pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;
  return pdfjs.getDocument({ data: new Uint8Array(data) }).promise;
}

async function extractLayout(pdf: PDFDocumentProxy): Promise<DocumentLayout> {
  const pdfjs = await import("pdfjs-dist");
  const pages: DocumentPageLayout[] = [];
  for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
    const page = await pdf.getPage(pageNumber);
    const viewport = page.getViewport({ scale: 1 });
    const content = await page.getTextContent();
    const blocks = content.items.flatMap((raw, index) => {
      if (!("str" in raw) || !raw.str.trim()) return [];
      const item = raw as typeof raw & {
        str: string;
        transform: number[];
        width: number;
        height: number;
        fontName: string;
        dir: string;
      };
      const transform = pdfjs.Util.transform(viewport.transform, item.transform);
      const height = Math.max(Math.abs(item.height), Math.hypot(transform[2], transform[3]), 1);
      const width = Math.max(Math.abs(item.width), 1);
      return [{
        id: `p${pageNumber}-b${index}`,
        text: item.str,
        x: Math.max(0, transform[4] / viewport.width),
        y: Math.max(0, (transform[5] - height) / viewport.height),
        width: Math.min(1, width / viewport.width),
        height: Math.min(1, height / viewport.height),
        fontSize: height,
        fontFamily: item.fontName || "sans-serif",
        direction: item.dir || "ltr",
      }];
    });
    pages.push({ width: viewport.width, height: viewport.height, blocks });
  }
  const fingerprint = pdf.fingerprints[0] || `${pdf.numPages}-${Date.now()}`;
  return { fingerprint, page_count: pdf.numPages, pages };
}

export function translatedBox(block: DocumentPageLayout["blocks"][number], rotation: number) {
  return rotation === 90
    ? { x: 1 - block.y - block.height, y: block.x, width: block.height, height: block.width }
    : rotation === 180
      ? { x: 1 - block.x - block.width, y: 1 - block.y - block.height, width: block.width, height: block.height }
      : rotation === 270
        ? { x: block.y, y: 1 - block.x - block.width, width: block.height, height: block.width }
        : block;
}

function PdfPage({
  pdf,
  pageNumber,
  zoom,
  layout,
  translated,
  showTranslation,
  rotation,
  onCurrent,
}: {
  pdf: PDFDocumentProxy;
  pageNumber: number;
  zoom: number;
  layout: DocumentPageLayout | undefined;
  translated: Map<string, string>;
  showTranslation: boolean;
  rotation: number;
  onCurrent: (page: number) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(pageNumber <= 2);
  const [size, setSize] = useState({ width: (layout?.width ?? 612) * zoom, height: (layout?.height ?? 792) * zoom });
  const [renderVersion, setRenderVersion] = useState(0);
  const [blockColors, setBlockColors] = useState<Record<string, { background: string; color: string }>>({});

  useEffect(() => {
    const root = rootRef.current;
    if (!root || visible || !("IntersectionObserver" in window)) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) setVisible(true);
    }, { rootMargin: "900px" });
    observer.observe(root);
    return () => observer.disconnect();
  }, [visible]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root || !("IntersectionObserver" in window)) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting && entry.intersectionRatio >= .45)) onCurrent(pageNumber);
    }, { threshold: [.45] });
    observer.observe(root);
    return () => observer.disconnect();
  }, [onCurrent, pageNumber]);

  useEffect(() => {
    let cancelled = false;
    let renderTask: { cancel: () => void; promise: Promise<unknown> } | null = null;
    if (!visible) return undefined;
    void pdf.getPage(pageNumber).then((page) => {
      if (cancelled || !canvasRef.current) return;
      const viewport = page.getViewport({ scale: zoom, rotation: page.rotate + rotation });
      const outputScale = window.devicePixelRatio || 1;
      const canvas = canvasRef.current;
      const context = canvas.getContext("2d");
      if (!context) return;
      canvas.width = Math.floor(viewport.width * outputScale);
      canvas.height = Math.floor(viewport.height * outputScale);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      setSize({ width: viewport.width, height: viewport.height });
      renderTask = page.render({
        canvasContext: context,
        viewport,
        transform: outputScale === 1 ? undefined : [outputScale, 0, 0, outputScale, 0, 0],
      });
      return renderTask.promise.then(() => {
        if (!cancelled) setRenderVersion((value) => value + 1);
      });
    }).catch((error) => {
      // PDF.js rejects the render promise when a page is unmounted or a zoom
      // change cancels the old task. Other render failures are surfaced by the
      // top-level document loader on the next retry instead of becoming an
      // unhandled promise rejection.
      if (!cancelled && error?.name !== "RenderingCancelledException") return;
    });
    return () => {
      cancelled = true;
      renderTask?.cancel();
    };
  }, [pageNumber, pdf, rotation, visible, zoom]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext("2d", { willReadFrequently: true });
    if (!showTranslation || !canvas || !context || !layout || renderVersion === 0) {
      if (!showTranslation) setBlockColors({});
      return;
    }
    const next: Record<string, { background: string; color: string }> = {};
    for (const block of layout.blocks) {
      const box = translatedBox(block, rotation);
      const insetX = Math.min(box.width * .12, .004);
      const insetY = Math.min(box.height * .18, .004);
      const points = [
        [box.x + insetX, box.y + insetY],
        [box.x + box.width - insetX, box.y + insetY],
        [box.x + insetX, box.y + box.height - insetY],
        [box.x + box.width - insetX, box.y + box.height - insetY],
      ];
      const samples = points.map(([x, y]) => context.getImageData(
        Math.max(0, Math.min(canvas.width - 1, Math.round(x * canvas.width))),
        Math.max(0, Math.min(canvas.height - 1, Math.round(y * canvas.height))),
        1,
        1,
      ).data);
      const [red, green, blue] = [0, 1, 2].map((channel) => Math.round(
        samples.reduce((sum, sample) => sum + sample[channel], 0) / samples.length,
      ));
      const luminance = .2126 * red + .7152 * green + .0722 * blue;
      next[block.id] = {
        background: `rgba(${red}, ${green}, ${blue}, .97)`,
        color: luminance < 115 ? "#f7f8f8" : "#171a19",
      };
    }
    setBlockColors(next);
  }, [layout, renderVersion, rotation, showTranslation]);

  return (
    <div className="document-pdf-page" ref={rootRef} style={{ width: size.width, height: size.height }}>
      <canvas ref={canvasRef} aria-label={`第 ${pageNumber} 页`} />
      {layout && (
        <div className="document-original-text-layer" aria-hidden="true">
          {layout.blocks.map((block) => {
            const box = translatedBox(block, rotation);
            return (
              <span
                key={block.id}
                dir={block.direction === "rtl" ? "rtl" : "ltr"}
                style={{
                  left: `${box.x * 100}%`,
                  top: `${box.y * 100}%`,
                  width: `${box.width * 100}%`,
                  height: `${Math.max(box.height * 100, 1.2)}%`,
                  fontSize: `${Math.max(2, block.fontSize * zoom)}px`,
                }}
              >{block.text}</span>
            );
          })}
        </div>
      )}
      {showTranslation && layout && (
        <div className="document-translation-layer" aria-label={`第 ${pageNumber} 页译文`}>
          {layout.blocks.map((block) => {
            const text = translated.get(block.id);
            if (!text) return null;
            const box = translatedBox(block, rotation);
            const expansion = text.length / Math.max(1, block.text.length);
            const fittedFontSize = block.fontSize * zoom * .82 / Math.max(1, Math.sqrt(expansion));
            return (
              <span
                key={block.id}
                title={text}
                dir={block.direction === "rtl" ? "rtl" : "ltr"}
                style={{
                  left: `${box.x * 100}%`,
                  top: `${box.y * 100}%`,
                  width: `${box.width * 100}%`,
                  height: `${Math.max(box.height * 100, 1.2)}%`,
                  fontSize: `${Math.max(7, fittedFontSize)}px`,
                  background: blockColors[block.id]?.background,
                  color: blockColors[block.id]?.color,
                }}
              >{text}</span>
            );
          })}
        </div>
      )}
      <span className="document-page-number">{pageNumber}</span>
    </div>
  );
}

export default function DocumentWorkspace({
  api,
  project,
  agentWidth,
  agentCollapsed,
  sessionBusy,
  sessionError,
  onAgentWidth,
  onAgentCollapsed,
  onDocumentAction,
}: DocumentWorkspaceProps) {
  const [manifest, setManifest] = useState<DocumentManifest | null>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [layout, setLayout] = useState<DocumentLayout | null>(null);
  const [translation, setTranslation] = useState<DocumentTranslation | null>(null);
  const [locale, setLocale] = useState("zh-CN");
  const [showTranslation, setShowTranslation] = useState(false);
  const [zoom, setZoom] = useState(1.2);
  const [rotation, setRotation] = useState(0);
  const [currentPage, setCurrentPage] = useState(1);
  const [view, setView] = useState<DocumentView>("document");
  const [blog, setBlog] = useState<DocumentBlog | null>(null);
  const [blogTouched, setBlogTouched] = useState(false);
  const [blogConflict, setBlogConflict] = useState<{ local: DocumentBlog; server: DocumentBlog } | null>(null);
  const [pendingAction, setPendingAction] = useState<"translate" | "generate_blog" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const previousBusy = useRef(sessionBusy);

  const refreshArtifacts = useCallback(async () => {
    try {
      const nextManifest = await api.documentManifest(project.path);
      setManifest(nextManifest);
      try {
        const nextTranslation = await api.documentTranslation(project.path, locale);
        setTranslation(nextTranslation);
        if (pendingAction === "translate") setPendingAction(null);
      } catch (reason) {
        if (!isMissing(reason)) throw reason;
        setTranslation(null);
        if (pendingAction === "translate") {
          setPendingAction(null);
          setError("翻译未生成，请查看右侧 Agent 的失败原因后重试。");
        }
      }
      try {
        const nextBlog = await api.documentBlog(project.path);
        setBlog(nextBlog);
        if (pendingAction === "generate_blog") setPendingAction(null);
      } catch (reason) {
        if (!isMissing(reason)) throw reason;
        setBlog(null);
        if (pendingAction === "generate_blog") {
          setPendingAction(null);
          setError("Blog 未生成，请查看右侧 Agent 的失败原因后重试。");
        }
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [api, locale, pendingAction, project.path]);

  useEffect(() => {
    let cancelled = false;
    let loadedPdf: PDFDocumentProxy | null = null;
    setLoading(true);
    setError(null);
    setPdf(null);
    setLayout(null);
    setTranslation(null);
    setBlog(null);
    setBlogConflict(null);
    void Promise.all([api.documentManifest(project.path), api.documentAsset(project.path)]).then(async ([nextManifest, data]) => {
      const nextPdf = await loadPdf(data);
      loadedPdf = nextPdf;
      const nextLayout = await extractLayout(nextPdf);
      if (cancelled) {
        await nextPdf.destroy();
        loadedPdf = null;
        return;
      }
      setManifest(nextManifest);
      setPdf(nextPdf);
      setLayout(nextLayout);
      if (sourceLooksChinese(nextLayout)) setLocale("en");
      if (!nextManifest.layout || nextManifest.layout.fingerprint !== nextLayout.fingerprint) {
        await api.saveDocumentLayout(project.path, nextLayout);
      }
      try {
        setBlog(await api.documentBlog(project.path));
      } catch (reason) {
        if (!isMissing(reason)) throw reason;
      }
    }).catch((reason) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
      if (loadedPdf) void loadedPdf.destroy();
    };
  }, [api, project.id, project.path]);

  useEffect(() => {
    setShowTranslation(false);
    setTranslation(null);
    void api.documentTranslation(project.path, locale).then(setTranslation).catch((reason) => {
      if (!isMissing(reason)) setError(reason instanceof Error ? reason.message : String(reason));
    });
  }, [api, locale, project.path]);

  useEffect(() => {
    const completed = previousBusy.current && !sessionBusy;
    previousBusy.current = sessionBusy;
    if (completed) {
      const timer = window.setTimeout(() => void refreshArtifacts(), 600);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [pendingAction, refreshArtifacts, sessionBusy]);

  useEffect(() => {
    if (!pendingAction || !sessionError || sessionBusy) return;
    setPendingAction(null);
    setError(sessionError);
  }, [pendingAction, sessionBusy, sessionError]);

  useEffect(() => {
    if (!blog || !blogTouched) return;
    const timer = window.setTimeout(() => {
      void api.saveDocumentBlog(project.path, blog).then((saved) => {
        setBlog(saved);
        setBlogTouched(false);
        setBlogConflict(null);
      }).catch(async (reason) => {
        const message = reason instanceof Error ? reason.message : String(reason);
        if (/changed since|conflict/i.test(message)) {
          try {
            const server = await api.documentBlog(project.path);
            setBlogConflict({ local: blog, server });
            setBlogTouched(false);
          } catch (refreshReason) {
            setError(refreshReason instanceof Error ? refreshReason.message : String(refreshReason));
          }
          return;
        }
        setError(message);
      });
    }, 500);
    return () => window.clearTimeout(timer);
  }, [api, blog, blogTouched, project.path]);

  const translated = useMemo(() => new Map(
    (translation?.blocks ?? []).map((block) => [block.id, block.translated_text]),
  ), [translation]);
  const scannedPages = layout?.pages.filter((page) => page.blocks.length === 0).length ?? 0;
  const selectCurrentPage = useCallback((page: number) => setCurrentPage(page), []);

  const startResize = (event: React.PointerEvent) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = agentWidth;
    const move = (next: PointerEvent) => onAgentWidth(Math.min(720, Math.max(320, startWidth + startX - next.clientX)));
    const finish = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", finish);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish);
  };

  return (
    <section className="document-workspace" aria-label={`${project.name} 文档工作区`}>
      <header className="document-toolbar">
        <div className="document-title" title={manifest?.source.name}>
          <FileText />
          <span><strong>{project.name}</strong><small>{manifest?.source.name ?? "正在读取文档"}</small></span>
        </div>
        <div className="document-view-tabs" role="tablist" aria-label="文档视图">
          <button className={view === "document" ? "active" : ""} onClick={() => setView("document")}><FileText />文档</button>
          <button className={view === "blog" ? "active" : ""} onClick={() => setView("blog")}><BookOpen />Blog</button>
        </div>
        {view === "document" && (
          <div className="document-tools">
            <span className="document-page-indicator">{currentPage} / {pdf?.numPages ?? manifest?.pdf.page_count ?? "–"}</span>
            <button className="icon-button small" title="缩小" onClick={() => setZoom((value) => Math.max(.6, value - .1))}><Minus /></button>
            <span className="document-zoom">{Math.round(zoom * 100)}%</span>
            <button className="icon-button small" title="放大" onClick={() => setZoom((value) => Math.min(2.5, value + .1))}><Plus /></button>
            <button className="icon-button small" title="顺时针旋转" onClick={() => setRotation((value) => (value + 90) % 360)}><RotateCw /></button>
            <select aria-label="翻译目标语言" value={locale} onChange={(event) => setLocale(event.target.value)}>
              {TARGET_LANGUAGES.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
            </select>
            <button
              className="document-action-button"
              disabled={!layout || layout.pages.every((page) => page.blocks.length === 0) || sessionBusy}
              onClick={() => {
                setPendingAction("translate");
                if (!onDocumentAction("translate", { locale })) setPendingAction(null);
              }}
            >{pendingAction === "translate" || (sessionBusy && !translation) ? <LoaderCircle className="spin" /> : <Languages />}翻译</button>
            <label className={`document-translation-toggle ${translation ? "ready" : ""}`}>
              <input
                type="checkbox"
                checked={showTranslation}
                disabled={!translation}
                onChange={(event) => setShowTranslation(event.target.checked)}
              />
              {showTranslation ? <Check /> : null}显示译文
            </label>
            <button
              className="document-action-button blog"
              disabled={!layout || sessionBusy || (showTranslation && !translation)}
              onClick={() => {
                setView("blog");
                setPendingAction("generate_blog");
                if (!onDocumentAction("generate_blog", { locale, source: showTranslation ? "translation" : "original" })) setPendingAction(null);
              }}
            ><Sparkles />生成 Blog</button>
          </div>
        )}
        <button className="icon-button document-agent-toggle" title="折叠 Agent" onClick={() => onAgentCollapsed(true)}>
          <PanelRightClose />
        </button>
      </header>

      {error && <div className="document-error"><AlertTriangle />{error}<button onClick={() => { setError(null); void refreshArtifacts(); }}><RefreshCw />重试</button></div>}
      {loading && <div className="document-loading"><LoaderCircle className="spin" />正在准备文档</div>}
      {!loading && view === "document" && (
        <div className="document-pdf-scroll">
          {scannedPages > 0 && (
            <div className="document-scan-warning"><AlertTriangle />检测到 {scannedPages} 个无文本页面，可阅读但暂不能原位翻译。</div>
          )}
          {pdf && Array.from({ length: pdf.numPages }, (_, index) => (
            <PdfPage
              key={index + 1}
              pdf={pdf}
              pageNumber={index + 1}
              zoom={zoom}
              layout={layout?.pages[index]}
              translated={translated}
              showTranslation={showTranslation}
              rotation={rotation}
              onCurrent={selectCurrentPage}
            />
          ))}
        </div>
      )}
      {!loading && view === "blog" && (
        <div className="document-blog-shell">
          {blogConflict && (
            <div className="document-blog-conflict">
              <AlertTriangle />
              <span>Blog 已被 Agent 或另一个窗口更新。请选择保留哪个版本。</span>
              <button onClick={() => {
                setBlog(blogConflict.server);
                setBlogConflict(null);
              }}>载入服务器版本</button>
              <button className="primary" onClick={() => {
                setBlog({ ...blogConflict.local, revision: blogConflict.server.revision });
                setBlogConflict(null);
                setBlogTouched(true);
              }}>保留我的版本</button>
            </div>
          )}
          {pendingAction === "generate_blog" && !blog ? (
            <div className="document-blog-empty"><LoaderCircle className="spin" /><h2>Agent 正在生成 Blog</h2><p>生成完成后会自动载入这里。</p></div>
          ) : blog ? (
            <div className="document-blog-columns">
              <textarea
                aria-label="Markdown Blog 编辑器"
                className="document-blog-editor"
                value={blog.markdown}
                spellCheck
                onChange={(event) => {
                  setBlog({ ...blog, markdown: event.target.value });
                  setBlogTouched(true);
                }}
              />
              <article className="document-blog-preview"><BlogPreview api={api} workspace={project.path} markdown={blog.markdown} /></article>
            </div>
          ) : (
            <div className="document-blog-empty"><BookOpen /><h2>还没有 Blog</h2><p>返回文档视图，选择原文或译文后生成。</p></div>
          )}
        </div>
      )}

      {!agentCollapsed && <button className="document-agent-resize" aria-label="调整 Agent 面板宽度" onPointerDown={startResize} />}
      {agentCollapsed && (
        <button className="document-agent-rail" title="展开 Agent" onClick={() => onAgentCollapsed(false)}>
          <PanelRightOpen /><span>Agent</span>
        </button>
      )}
    </section>
  );
}
