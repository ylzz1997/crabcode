import { useEffect, useState } from "react";
import { BookOpen, Check, EllipsisVertical, FileText, Folder, GitCompareArrows } from "lucide-react";
import { ReviewMenu } from "./ReviewMenu";

type WorkspaceView = "document" | "blog" | "files" | "changes";
type SecondaryView = Exclude<WorkspaceView, "document">;

const SECONDARY_VIEWS = [
  { value: "blog", label: "Blog", icon: BookOpen },
  { value: "files", label: "文件", icon: Folder },
  { value: "changes", label: "变更", icon: GitCompareArrows },
] as const;

export function DocumentViewTabs({ activeView, filesAvailable, changeCount, onViewChange }: {
  activeView: WorkspaceView;
  filesAvailable: boolean;
  changeCount: number;
  onViewChange: (view: WorkspaceView) => void;
}) {
  const [lastSecondaryView, setLastSecondaryView] = useState<SecondaryView>("blog");
  useEffect(() => {
    if (activeView !== "document") setLastSecondaryView(activeView);
  }, [activeView]);

  const secondaryView = activeView !== "document" ? activeView
    : filesAvailable ? lastSecondaryView : "blog";
  const selected = SECONDARY_VIEWS.find((item) => item.value === secondaryView)!;
  const SecondaryIcon = selected.icon;
  const choose = (view: SecondaryView) => {
    setLastSecondaryView(view);
    onViewChange(view);
  };

  return <div className="document-view-tabs">
    <div className="document-view-tablist" role="tablist" aria-label="文档视图">
      <button type="button" role="tab" aria-selected={activeView === "document"}
        className={activeView === "document" ? "active" : ""} onClick={() => onViewChange("document")}>
        <FileText />文档
      </button>
      <button type="button" role="tab" aria-selected={activeView !== "document"}
        className={activeView !== "document" ? "active" : ""} onClick={() => choose(secondaryView)}>
        <SecondaryIcon />{selected.label}
        {secondaryView === "changes" && changeCount > 0 && <small className="document-change-count">{changeCount}</small>}
      </button>
    </div>
    <ReviewMenu label={null} ariaLabel="切换工作区视图" width={160}
      triggerIcon={<EllipsisVertical aria-hidden="true" />} triggerClassName="document-view-menu-trigger">
      {(close) => SECONDARY_VIEWS.filter((item) => filesAvailable || item.value === "blog").map(({ value, label, icon: Icon }) => (
        <button key={value} type="button" className="document-view-menu-item" data-menu-option
          role="menuitemradio" aria-checked={secondaryView === value}
          onClick={() => { choose(value); close(); }}>
          <span className="document-view-menu-option"><Icon />{label}</span>
          {secondaryView === value && <Check aria-hidden="true" />}
        </button>
      ))}
    </ReviewMenu>
  </div>;
}
