import {
  AlertTriangle,
  BookOpen,
  Check,
  FileText,
  Languages,
  LoaderCircle,
  Minus,
  MoreHorizontal,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  RefreshCw,
  RotateCw,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { createPortal } from "react-dom";
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
  DocumentViewState,
  ProjectPreset,
} from "./types";

type DocumentView = "document" | "blog";

const DOCUMENT_ZOOM_MIN = .6;
const DOCUMENT_ZOOM_MAX = 2.5;
const DOCUMENT_ZOOM_STEP = .1;

export function clampDocumentZoom(value: number): number {
  return Math.round(Math.min(DOCUMENT_ZOOM_MAX, Math.max(DOCUMENT_ZOOM_MIN, value)) * 10) / 10;
}

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
  connectionId: string;
  project: ProjectPreset;
  documentView?: DocumentViewState;
  agentWidth: number;
  agentCollapsed: boolean;
  showOriginalText: boolean;
  translationConcurrency: number;
  translationBatchSize: number;
  sessionBusy: boolean;
  sessionError: string | null;
  onAgentWidth: (width: number) => void;
  onAgentCollapsed: (collapsed: boolean) => void;
  onDocumentViewState: (connectionId: string, projectId: string, state: DocumentViewState) => void;
  onDocumentAction: (
    action: "translate" | "generate_blog",
    options: {
      locale?: string;
      source?: "original" | "translation";
      translation_concurrency?: number;
      translation_batch_size?: number;
    },
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

function DocumentActionsMenu({
  locale,
  sessionBusy,
  translating,
  clearing,
  generatingBlog,
  translateDisabled,
  clearDisabled,
  blogDisabled,
  onLocale,
  onTranslate,
  onClear,
  onGenerateBlog,
}: {
  locale: string;
  sessionBusy: boolean;
  translating: boolean;
  clearing: boolean;
  generatingBlog: boolean;
  translateDisabled: boolean;
  clearDisabled: boolean;
  blogDisabled: boolean;
  onLocale: (locale: string) => void;
  onTranslate: () => void;
  onClear: () => void;
  onGenerateBlog: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const placeMenu = useCallback(() => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const viewportPadding = 8;
    const menuWidth = Math.min(236, window.innerWidth - viewportPadding * 2);
    const menuHeight = 205;
    setPosition({
      top: Math.min(rect.bottom + 7, Math.max(viewportPadding, window.innerHeight - menuHeight - viewportPadding)),
      left: Math.max(viewportPadding, Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - viewportPadding)),
    });
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    const onViewportChange = () => placeMenu();
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", onViewportChange);
    window.addEventListener("scroll", onViewportChange, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onViewportChange);
      window.removeEventListener("scroll", onViewportChange, true);
    };
  }, [open, placeMenu]);

  const choose = (action: () => void) => {
    setOpen(false);
    action();
  };
  const pending = translating || clearing || generatingBlog;

  return (
    <>
      <button
        ref={triggerRef}
        className="icon-button small document-actions-trigger"
        type="button"
        title="文档操作"
        aria-label="文档操作"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => {
          if (!open) placeMenu();
          setOpen((value) => !value);
        }}
      >
        {pending ? <LoaderCircle className="spin" /> : <MoreHorizontal />}
      </button>
      {open && createPortal(
        <div
          ref={menuRef}
          className="document-actions-menu"
          role="menu"
          aria-label="文档操作"
          style={position}
        >
          <label className="document-actions-locale">
            <Languages />
            <span>目标语言</span>
            <select
              aria-label="翻译目标语言"
              value={locale}
              disabled={sessionBusy}
              onChange={(event) => onLocale(event.target.value)}
            >
              {TARGET_LANGUAGES.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
            </select>
          </label>
          <button type="button" role="menuitem" disabled={translateDisabled} onClick={() => choose(onTranslate)}>
            {translating ? <LoaderCircle className="spin" /> : <Languages />}
            <span>翻译文档</span>
          </button>
          <button className="danger" type="button" role="menuitem" disabled={clearDisabled} onClick={() => choose(onClear)}>
            {clearing ? <LoaderCircle className="spin" /> : <Trash2 />}
            <span>清空翻译缓存</span>
          </button>
          <div className="document-actions-separator" role="separator" />
          <button className="blog" type="button" role="menuitem" disabled={blogDisabled} onClick={() => choose(onGenerateBlog)}>
            {generatingBlog ? <LoaderCircle className="spin" /> : <Sparkles />}
            <span>生成 Blog</span>
          </button>
        </div>,
        document.body,
      )}
    </>
  );
}

async function loadPdf(data: ArrayBuffer): Promise<PDFDocumentProxy> {
  const pdfjs = await import("pdfjs-dist");
  pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;
  return pdfjs.getDocument({ data: new Uint8Array(data) }).promise;
}

const DOCUMENT_LAYOUT_VERSION = "paragraph-v5";
type TextBlock = DocumentPageLayout["blocks"][number];

interface TextLine {
  blocks: TextBlock[];
  text: string;
  x: number;
  y: number;
  width: number;
  height: number;
  fontSize: number;
  fontFamily: string;
  direction: string;
}

function median(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[middle - 1] + sorted[middle]) / 2
    : sorted[middle];
}

function range(values: number[]): number {
  return values.length === 0 ? 0 : Math.max(...values) - Math.min(...values);
}

function joinInlineText(left: string, right: string, gap: number, characterWidth: number): string {
  if (!left) return right;
  if (!right) return left;
  if (/\s$/.test(left) || /^\s/.test(right)) return `${left}${right}`;
  if (/^[=<>≈≠≤≥±×÷∼∝]/.test(right)) return `${left} ${right}`;
  if (/[=<>≈≠≤≥±×÷∼∝]$/.test(left)) {
    return /^\d/.test(right) ? `${left}${right}` : `${left} ${right}`;
  }
  if (/[*∗†‡]$/.test(left) && /^\p{L}/u.test(right)) return `${left} ${right}`;
  if (/^[,.;:!?%)}\]]/.test(right) || /[({\[]$/.test(left)) return `${left}${right}`;
  return gap <= characterWidth * .45 ? `${left}${right}` : `${left} ${right}`;
}

function joinLineText(left: string, right: string): string {
  if (!left) return right;
  if (!right) return left;
  if (/-$/.test(left) && /^\p{L}/u.test(right)) return `${left}${right}`;
  if (/[/({\[]$/.test(left) || /^[,.;:!?%)}\]]/.test(right)) return `${left}${right}`;
  return `${left} ${right}`;
}

function makeTextLine(blocks: TextBlock[]): TextLine {
  const ordered = [...blocks].sort((left, right) => left.x - right.x);
  const x = Math.min(...ordered.map((block) => block.x));
  const y = Math.min(...ordered.map((block) => block.y));
  const right = Math.max(...ordered.map((block) => block.x + block.width));
  const bottom = Math.max(...ordered.map((block) => block.y + block.height));
  const characterWidth = median(ordered.map((block) => (
    block.width / Math.max(1, Array.from(block.text.trim()).length)
  )));
  let text = "";
  let previous: TextBlock | null = null;
  for (const block of ordered) {
    text = joinInlineText(
      text,
      block.text,
      previous ? block.x - (previous.x + previous.width) : 0,
      characterWidth,
    );
    previous = block;
  }
  const dominant = [...ordered].sort((left, right) => right.width - left.width)[0];
  return {
    blocks: ordered,
    text: text.trim(),
    x,
    y,
    width: right - x,
    height: bottom - y,
    fontSize: Math.max(...ordered.map((block) => block.fontSize)),
    fontFamily: dominant?.fontFamily || "sans-serif",
    direction: dominant?.direction || "ltr",
  };
}

function textRows(blocks: TextBlock[]): TextBlock[][] {
  const rows: TextBlock[][] = [];
  for (const block of [...blocks].sort((left, right) => (
    left.y - right.y || left.x - right.x
  ))) {
    const bottom = block.y + block.height;
    let best: TextBlock[] | null = null;
    let bestDistance = Number.POSITIVE_INFINITY;
    for (const row of rows) {
      const rowBottom = median(row.map((item) => item.y + item.height));
      const rowHeight = median(row.map((item) => item.height));
      const distance = Math.abs(bottom - rowBottom);
      if (distance <= Math.max(block.height, rowHeight) * .45 && distance < bestDistance) {
        best = row;
        bestDistance = distance;
      }
    }
    if (best) best.push(block);
    else rows.push([block]);
  }
  return rows.sort((left, right) => (
    Math.min(...left.map((block) => block.y)) - Math.min(...right.map((block) => block.y))
  ));
}

function textLines(blocks: TextBlock[]): TextLine[] {
  const lines: TextLine[] = [];
  for (const row of textRows(blocks)) {
    const ordered = [...row].sort((left, right) => left.x - right.x);
    const characterWidth = median(ordered.map((block) => (
      block.width / Math.max(1, Array.from(block.text.trim()).length)
    )));
    const splitGap = Math.max(.012, Math.min(.03, characterWidth * 2.5));
    let segment: TextBlock[] = [];
    for (const block of ordered) {
      const previous = segment.at(-1);
      if (previous && block.x - (previous.x + previous.width) > splitGap) {
        lines.push(makeTextLine(segment));
        segment = [];
      }
      segment.push(block);
    }
    if (segment.length > 0) lines.push(makeTextLine(segment));
  }
  return lines.sort((left, right) => left.y - right.y || left.x - right.x);
}

function horizontalOverlap(left: TextLine, right: TextLine): number {
  const overlap = Math.max(0, Math.min(left.x + left.width, right.x + right.width) - Math.max(left.x, right.x));
  return overlap / Math.max(.0001, Math.min(left.width, right.width));
}

function canAppendLine(lines: TextLine[], line: TextLine): boolean {
  const previous = lines.at(-1);
  if (!previous) return false;
  const height = Math.max(previous.height, line.height);
  const verticalGap = line.y - (previous.y + previous.height);
  if (verticalGap < -height * .2 || verticalGap > height * .85) return false;
  const fontRatio = Math.max(previous.fontSize, line.fontSize) / Math.max(1, Math.min(previous.fontSize, line.fontSize));
  if (fontRatio > 1.38) return false;

  const centerDistance = Math.abs(
    (previous.x + previous.width / 2) - (line.x + line.width / 2),
  );
  if (
    horizontalOverlap(previous, line) < .5
    && Math.abs(previous.x - line.x) > .035
    && centerDistance > .035
  ) return false;

  const widthRatio = Math.min(previous.width, line.width) / Math.max(previous.width, line.width);
  if (
    lines.length === 1
    && widthRatio < .4
    && !(centerDistance <= .025 && Math.max(previous.width, line.width) < .35)
  ) return false;

  if (lines.length >= 2) {
    const typicalWidth = median(lines.map((item) => item.width));
    const typicalLeft = median(lines.map((item) => item.x));
    const typicalFontSize = median(lines.map((item) => item.fontSize));
    if (previous.width < typicalWidth * .68 && line.x <= typicalLeft + .03) return false;
    if (line.width < typicalWidth * .45 && line.fontSize < typicalFontSize * .94) return false;
    if (line.x > typicalLeft + .035 && previous.width > typicalWidth * .8) return false;
  }
  return true;
}

function paragraphAlignment(lines: TextLine[]): "left" | "center" | "right" {
  const lefts = lines.map((line) => line.x);
  const rights = lines.map((line) => line.x + line.width);
  const centers = lines.map((line) => line.x + line.width / 2);
  if (lines.length === 1) {
    return Math.abs(centers[0] - .5) <= .04 && lines[0].width < .7 ? "center" : "left";
  }
  if (range(centers) <= .035 && range(lefts) >= .018) return "center";
  if (range(rights) <= .018 && range(lefts) >= .025) return "right";
  return "left";
}

export function groupTextBlocksIntoParagraphs(blocks: TextBlock[], pageNumber: number): TextBlock[] {
  const paragraphs: TextLine[][] = [];
  for (const line of textLines(blocks)) {
    const candidates = paragraphs
      .map((lines, index) => ({ lines, index }))
      .filter(({ lines }) => canAppendLine(lines, line))
      .sort((left, right) => {
        const leftLast = left.lines.at(-1)!;
        const rightLast = right.lines.at(-1)!;
        const leftScore = Math.abs(line.y - (leftLast.y + leftLast.height)) + Math.abs(line.x - leftLast.x) * .2;
        const rightScore = Math.abs(line.y - (rightLast.y + rightLast.height)) + Math.abs(line.x - rightLast.x) * .2;
        return leftScore - rightScore;
      });
    if (candidates.length > 0) candidates[0].lines.push(line);
    else paragraphs.push([line]);
  }

  return paragraphs
    .sort((left, right) => left[0].y - right[0].y || left[0].x - right[0].x)
    .map((lines, index) => {
      const x = Math.min(...lines.map((line) => line.x));
      const y = Math.min(...lines.map((line) => line.y));
      const right = Math.max(...lines.map((line) => line.x + line.width));
      const bottom = Math.max(...lines.map((line) => line.y + line.height));
      const dominant = [...lines].sort((left, right) => right.width - left.width)[0];
      return {
        id: `p${pageNumber}-${DOCUMENT_LAYOUT_VERSION}-b${index}`,
        text: lines.reduce((text, line) => joinLineText(text, line.text), ""),
        x,
        y,
        width: right - x,
        height: bottom - y,
        fontSize: median(lines.map((line) => line.fontSize)),
        fontFamily: dominant.fontFamily,
        direction: dominant.direction,
        textAlign: paragraphAlignment(lines),
      };
    });
}

async function extractLayout(pdf: PDFDocumentProxy): Promise<DocumentLayout> {
  const pdfjs = await import("pdfjs-dist");
  const pages: DocumentPageLayout[] = [];
  for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
    const page = await pdf.getPage(pageNumber);
    const viewport = page.getViewport({ scale: 1 });
    const content = await page.getTextContent();
    const rawBlocks = content.items.flatMap((raw, index) => {
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
      if (Math.abs(transform[1]) > Math.abs(transform[0]) * .25) return [];
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
    const blocks = groupTextBlocksIntoParagraphs(rawBlocks, pageNumber);
    pages.push({ width: viewport.width, height: viewport.height, blocks });
  }
  const sourceFingerprint = pdf.fingerprints[0] || `${pdf.numPages}-${Date.now()}`;
  const fingerprint = `${sourceFingerprint}:${DOCUMENT_LAYOUT_VERSION}`;
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

function TranslationOverlayBlock({
  block,
  text,
  box,
  zoom,
  pageWidth,
  pageHeight,
  colors,
}: {
  block: TextBlock;
  text: string;
  box: { x: number; y: number; width: number; height: number };
  zoom: number;
  pageWidth: number;
  pageHeight: number;
  colors?: { background: string; color: string };
}) {
  const ref = useRef<HTMLSpanElement>(null);

  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const maximum = Math.max(5, block.fontSize * zoom * .94);
    const minimum = Math.min(maximum, Math.max(3.5, maximum * .38));
    const fits = () => (
      node.scrollWidth <= node.clientWidth + 1
      && node.scrollHeight <= node.clientHeight + 1
    );
    node.style.fontSize = `${maximum}px`;
    if (fits()) return;
    let low = minimum;
    let high = maximum;
    for (let attempt = 0; attempt < 9; attempt += 1) {
      const candidate = (low + high) / 2;
      node.style.fontSize = `${candidate}px`;
      if (fits()) low = candidate;
      else high = candidate;
    }
    node.style.fontSize = `${low}px`;
  }, [block.fontSize, block.id, pageHeight, pageWidth, text, zoom]);

  return (
    <span
      ref={ref}
      title={text}
      dir={block.direction === "rtl" ? "rtl" : "ltr"}
      style={{
        left: `${box.x * 100}%`,
        top: `${box.y * 100}%`,
        width: `${box.width * 100}%`,
        height: `${Math.max(box.height * 100, 1.2)}%`,
        background: colors?.background,
        color: colors?.color,
        textAlign: block.textAlign ?? "left",
      }}
    >{text}</span>
  );
}

function PdfPage({
  pdf,
  pageNumber,
  zoom,
  layout,
  translated,
  showOriginalText,
  showTranslation,
  rotation,
  onCurrent,
}: {
  pdf: PDFDocumentProxy;
  pageNumber: number;
  zoom: number;
  layout: DocumentPageLayout | undefined;
  translated: Map<string, string>;
  showOriginalText: boolean;
  showTranslation: boolean;
  rotation: number;
  onCurrent: (page: number) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textLayerRef = useRef<HTMLDivElement>(null);
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
    const container = textLayerRef.current;
    if (!visible || !container) {
      container?.replaceChildren();
      return undefined;
    }
    let cancelled = false;
    let textLayer: { cancel: () => void; render: () => Promise<unknown> } | null = null;
    container.replaceChildren();
    void Promise.all([pdf.getPage(pageNumber), import("pdfjs-dist")]).then(async ([page, pdfjs]) => {
      if (cancelled) return;
      const viewport = page.getViewport({ scale: zoom, rotation: page.rotate + rotation });
      textLayer = new pdfjs.TextLayer({
        textContentSource: page.streamTextContent(),
        container,
        viewport,
      });
      await textLayer.render();
    }).catch((reason) => {
      if (!cancelled && reason?.name !== "AbortException") container.replaceChildren();
    });
    return () => {
      cancelled = true;
      textLayer?.cancel();
      container.replaceChildren();
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
      <div
        ref={textLayerRef}
        className={`document-original-text-layer ${showOriginalText ? "show-text" : ""}`}
        aria-hidden="true"
        style={{ "--scale-factor": zoom } as CSSProperties}
      />
      {showTranslation && layout && (
        <div className="document-translation-layer" aria-label={`第 ${pageNumber} 页译文`}>
          {layout.blocks.map((block) => {
            const text = translated.get(block.id);
            if (!text) return null;
            const box = translatedBox(block, rotation);
            return (
              <TranslationOverlayBlock
                key={block.id}
                block={block}
                text={text}
                box={box}
                zoom={zoom}
                pageWidth={size.width}
                pageHeight={size.height}
                colors={blockColors[block.id]}
              />
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
  connectionId,
  project,
  documentView,
  agentWidth,
  agentCollapsed,
  showOriginalText,
  translationConcurrency,
  translationBatchSize,
  sessionBusy,
  sessionError,
  onAgentWidth,
  onAgentCollapsed,
  onDocumentViewState,
  onDocumentAction,
}: DocumentWorkspaceProps) {
  const [manifest, setManifest] = useState<DocumentManifest | null>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [layout, setLayout] = useState<DocumentLayout | null>(null);
  const [translation, setTranslation] = useState<DocumentTranslation | null>(null);
  const [locale, setLocale] = useState("zh-CN");
  const [showTranslation, setShowTranslation] = useState(false);
  const [zoom, setZoom] = useState(() => clampDocumentZoom(documentView?.zoom ?? 1.2));
  const [rotation, setRotation] = useState(0);
  const [currentPage, setCurrentPage] = useState(1);
  const [view, setView] = useState<DocumentView>("document");
  const [blog, setBlog] = useState<DocumentBlog | null>(null);
  const [blogTouched, setBlogTouched] = useState(false);
  const [blogConflict, setBlogConflict] = useState<{ local: DocumentBlog; server: DocumentBlog } | null>(null);
  const [pendingAction, setPendingAction] = useState<"translate" | "generate_blog" | null>(null);
  const [clearTranslationConfirm, setClearTranslationConfirm] = useState(false);
  const [clearingTranslation, setClearingTranslation] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const previousBusy = useRef(sessionBusy);
  const scrollRef = useRef<HTMLDivElement>(null);
  const zoomRef = useRef(zoom);
  const projectKey = `${connectionId}:${project.id}:${project.path}`;
  const projectKeyRef = useRef(projectKey);
  const viewIdentityRef = useRef({ connectionId, projectId: project.id });
  const restoredProjectKeyRef = useRef<string | null>(null);
  const restoreFrameRef = useRef<number>(0);
  const persistTimerRef = useRef<number | null>(null);
  const latestScrollRef = useRef({
    top: Math.max(0, documentView?.scroll_top ?? 0),
    left: Math.max(0, documentView?.scroll_left ?? 0),
  });
  const onDocumentViewStateRef = useRef(onDocumentViewState);
  onDocumentViewStateRef.current = onDocumentViewState;

  const persistViewState = useCallback((immediate = false) => {
    const commit = () => {
      persistTimerRef.current = null;
      const element = scrollRef.current;
      const top = Math.max(0, element?.scrollTop ?? latestScrollRef.current.top);
      const left = Math.max(0, element?.scrollLeft ?? latestScrollRef.current.left);
      latestScrollRef.current = { top, left };
      const identity = viewIdentityRef.current;
      onDocumentViewStateRef.current(identity.connectionId, identity.projectId, {
        zoom: zoomRef.current,
        scroll_top: top,
        scroll_left: left,
      });
    };
    if (immediate) {
      if (persistTimerRef.current !== null) window.clearTimeout(persistTimerRef.current);
      commit();
      return;
    }
    if (persistTimerRef.current !== null) window.clearTimeout(persistTimerRef.current);
    persistTimerRef.current = window.setTimeout(commit, 250);
  }, []);

  const changeZoom = useCallback((delta: number, anchor?: { clientX: number; clientY: number }) => {
    const previousZoom = zoomRef.current;
    const nextZoom = clampDocumentZoom(previousZoom + delta);
    if (nextZoom === previousZoom) return;
    const element = scrollRef.current;
    const rect = element?.getBoundingClientRect();
    const anchorX = anchor?.clientX ?? (rect ? rect.left + rect.width / 2 : 0);
    const anchorY = anchor?.clientY ?? (rect ? rect.top + rect.height / 2 : 0);
    const relativeX = rect ? anchorX - rect.left : 0;
    const relativeY = rect ? anchorY - rect.top : 0;
    const contentX = (element?.scrollLeft ?? 0) + relativeX;
    const contentY = (element?.scrollTop ?? 0) + relativeY;
    zoomRef.current = nextZoom;
    setZoom(nextZoom);
    persistViewState();
    // Keep the point under the cursor stable while the page dimensions catch up.
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        const current = scrollRef.current;
        if (!current) return;
        const ratio = nextZoom / previousZoom;
        current.scrollLeft = Math.max(0, contentX * ratio - relativeX);
        current.scrollTop = Math.max(0, contentY * ratio - relativeY);
        persistViewState(true);
      });
    });
  }, [persistViewState]);

  useLayoutEffect(() => {
    if (projectKeyRef.current === projectKey) return;
    persistViewState(true);
    projectKeyRef.current = projectKey;
    viewIdentityRef.current = { connectionId, projectId: project.id };
    const nextZoom = clampDocumentZoom(documentView?.zoom ?? 1.2);
    zoomRef.current = nextZoom;
    setZoom(nextZoom);
    latestScrollRef.current = {
      top: Math.max(0, documentView?.scroll_top ?? 0),
      left: Math.max(0, documentView?.scroll_left ?? 0),
    };
    restoredProjectKeyRef.current = null;
    setRotation(0);
    setCurrentPage(1);
  }, [connectionId, documentView, persistViewState, project.id, projectKey]);

  useLayoutEffect(() => {
    if (loading || !pdf || view !== "document" || restoredProjectKeyRef.current === projectKey) return undefined;
    const element = scrollRef.current;
    if (!element) return undefined;
    restoredProjectKeyRef.current = projectKey;
    const firstFrame = window.requestAnimationFrame(() => {
      const secondFrame = window.requestAnimationFrame(() => {
        const current = scrollRef.current;
        if (!current) return;
        current.scrollTop = latestScrollRef.current.top;
        current.scrollLeft = latestScrollRef.current.left;
        persistViewState(true);
      });
      restoreFrameRef.current = secondFrame;
    });
    restoreFrameRef.current = firstFrame;
    return () => {
      window.cancelAnimationFrame(restoreFrameRef.current);
    };
  }, [layout, loading, pdf, persistViewState, projectKey, view]);

  useEffect(() => () => {
    if (persistTimerRef.current !== null) window.clearTimeout(persistTimerRef.current);
    persistViewState(true);
  }, [persistViewState]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (view !== "document" || !event.altKey || event.ctrlKey || event.metaKey) return;
      const target = event.target;
      if (target instanceof HTMLElement && (
        target.isContentEditable
        || target.tagName === "INPUT"
        || target.tagName === "TEXTAREA"
        || target.tagName === "SELECT"
      )) return;
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        changeZoom(DOCUMENT_ZOOM_STEP);
      } else if (event.key === "-" || event.key === "_") {
        event.preventDefault();
        changeZoom(-DOCUMENT_ZOOM_STEP);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [changeZoom, view]);

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
      let resolvedManifest = nextManifest;
      if (!nextManifest.layout || nextManifest.layout.fingerprint !== nextLayout.fingerprint) {
        await api.saveDocumentLayout(project.path, nextLayout);
        resolvedManifest = await api.documentManifest(project.path);
      }
      if (cancelled) {
        await nextPdf.destroy();
        loadedPdf = null;
        return;
      }
      setManifest(resolvedManifest);
      setPdf(nextPdf);
      setLayout(nextLayout);
      if (sourceLooksChinese(nextLayout)) setLocale("en");
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
    if (!layout) return;
    void api.documentTranslation(project.path, locale).then((nextTranslation) => {
      if (
        nextTranslation.layout_fingerprint
        && nextTranslation.layout_fingerprint !== layout.fingerprint
      ) return;
      setTranslation(nextTranslation);
    }).catch((reason) => {
      if (!isMissing(reason)) setError(reason instanceof Error ? reason.message : String(reason));
    });
  }, [api, layout, locale, project.path]);

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
  const handlePdfScroll = useCallback(() => {
    const element = scrollRef.current;
    if (element) latestScrollRef.current = { top: element.scrollTop, left: element.scrollLeft };
    persistViewState();
  }, [persistViewState]);
  const handlePdfWheel = useCallback((event: React.WheelEvent<HTMLDivElement>) => {
    if (!event.altKey || event.deltaY === 0) return;
    event.preventDefault();
    changeZoom(event.deltaY < 0 ? DOCUMENT_ZOOM_STEP : -DOCUMENT_ZOOM_STEP, {
      clientX: event.clientX,
      clientY: event.clientY,
    });
  }, [changeZoom]);

  const clearTranslationCache = useCallback(async () => {
    setClearingTranslation(true);
    setError(null);
    try {
      await api.clearDocumentTranslation(project.path, locale);
      setTranslation(null);
      setShowTranslation(false);
      setPendingAction(null);
      setManifest(await api.documentManifest(project.path));
      setClearTranslationConfirm(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setClearingTranslation(false);
    }
  }, [api, locale, project.path]);

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
            <button className="icon-button small" title="缩小 (Alt+-)" aria-label="缩小" onClick={() => changeZoom(-DOCUMENT_ZOOM_STEP)}><Minus /></button>
            <span className="document-zoom">{Math.round(zoom * 100)}%</span>
            <button className="icon-button small" title="放大 (Alt++)" aria-label="放大" onClick={() => changeZoom(DOCUMENT_ZOOM_STEP)}><Plus /></button>
            <button className="icon-button small" title="顺时针旋转" onClick={() => setRotation((value) => (value + 90) % 360)}><RotateCw /></button>
            <DocumentActionsMenu
              locale={locale}
              sessionBusy={sessionBusy}
              translating={pendingAction === "translate"}
              clearing={clearingTranslation}
              generatingBlog={pendingAction === "generate_blog"}
              translateDisabled={!layout || layout.pages.every((page) => page.blocks.length === 0) || sessionBusy}
              clearDisabled={sessionBusy || clearingTranslation}
              blogDisabled={!layout || sessionBusy || (showTranslation && !translation)}
              onLocale={setLocale}
              onTranslate={() => {
                setPendingAction("translate");
                if (!onDocumentAction("translate", {
                  locale,
                  translation_concurrency: translationConcurrency,
                  translation_batch_size: translationBatchSize,
                })) setPendingAction(null);
              }}
              onClear={() => setClearTranslationConfirm(true)}
              onGenerateBlog={() => {
                setView("blog");
                setPendingAction("generate_blog");
                if (!onDocumentAction("generate_blog", { locale, source: showTranslation ? "translation" : "original" })) setPendingAction(null);
              }}
            />
            <label className={`document-translation-toggle ${translation ? "ready" : ""}`}>
              <input
                type="checkbox"
                checked={showTranslation}
                disabled={!translation}
                onChange={(event) => setShowTranslation(event.target.checked)}
              />
              {showTranslation ? <Check /> : null}显示译文
            </label>
          </div>
        )}
        <button className="icon-button document-agent-toggle" title="折叠 Agent" onClick={() => onAgentCollapsed(true)}>
          <PanelRightClose />
        </button>
      </header>

      {clearTranslationConfirm && createPortal((
        <div
          className="modal-backdrop"
          role="presentation"
          onMouseDown={(event) => event.target === event.currentTarget && !clearingTranslation && setClearTranslationConfirm(false)}
        >
          <section className="modal" role="dialog" aria-modal="true" aria-label="清空翻译缓存">
            <header>
              <h2>清空翻译缓存</h2>
              <button
                className="icon-button"
                type="button"
                title="关闭"
                disabled={clearingTranslation}
                onClick={() => setClearTranslationConfirm(false)}
              ><X /></button>
            </header>
            <div className="modal-body">
              <div className="confirm-dialog-copy">
                <Trash2 />
                <div>
                  <strong>清空{TARGET_LANGUAGES.find(([value]) => value === locale)?.[1] ?? locale}译文？</strong>
                  <p>已生成译文和未完成的翻译进度都会删除。原文和 Blog 不受影响。</p>
                </div>
              </div>
              <div className="modal-actions">
                <button type="button" disabled={clearingTranslation} onClick={() => setClearTranslationConfirm(false)}>取消</button>
                <button className="confirm-danger" type="button" disabled={clearingTranslation} onClick={() => void clearTranslationCache()}>
                  {clearingTranslation ? <LoaderCircle className="spin" /> : <Trash2 />}清空
                </button>
              </div>
            </div>
          </section>
        </div>
      ), document.body)}

      {error && <div className="document-error"><AlertTriangle />{error}<button onClick={() => { setError(null); void refreshArtifacts(); }}><RefreshCw />重试</button></div>}
      {loading && <div className="document-loading"><LoaderCircle className="spin" />正在准备文档</div>}
      {!loading && view === "document" && (
        <div
          ref={scrollRef}
          className="document-pdf-scroll"
          onScroll={handlePdfScroll}
          onWheel={handlePdfWheel}
        >
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
              showOriginalText={showOriginalText}
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
