export interface ChatScroll {
  isFollowing(): boolean;
  jumpToBottom(): void;
  dispose(): void;
}

export function bindChatScroll(viewport: HTMLElement, content: HTMLElement): ChatScroll;
