#import <AppKit/AppKit.h>
#import <dispatch/dispatch.h>
#import <pthread.h>
#import <stdbool.h>

// The executor is the app that owns the TCC permission. Keeping the drag source
// here lets Desktop, VS Code and CLI all offer the same installed .app file.
@interface CrabPermissionDragView : NSView <NSDraggingSource>
@property(nonatomic, copy) NSString *applicationPath;
@property(nonatomic, strong) NSEvent *mouseDownEvent;
@property(nonatomic) BOOL dragging;
@end

@implementation CrabPermissionDragView

- (BOOL)acceptsFirstMouse:(NSEvent *)event {
    return YES;
}

- (void)drawRect:(NSRect)dirtyRect {
    [super drawRect:dirtyRect];
    NSRect bounds = self.bounds;
    NSBezierPath *outline = [NSBezierPath bezierPathWithRoundedRect:NSInsetRect(bounds, 1, 1)
                                                            xRadius:10 yRadius:10];
    [[NSColor controlBackgroundColor] setFill];
    [outline fill];
    [[NSColor separatorColor] setStroke];
    [outline stroke];

    NSImage *icon = [[NSWorkspace sharedWorkspace] iconForFile:self.applicationPath];
    [icon drawInRect:NSMakeRect(15, 15, 48, 48)];
    NSDictionary *attributes = @{
        NSFontAttributeName: [NSFont systemFontOfSize:15 weight:NSFontWeightMedium],
        NSForegroundColorAttributeName: [NSColor labelColor]
    };
    [@"Crab Computer Use.app" drawInRect:NSMakeRect(76, 28, NSWidth(bounds) - 88, 24)
                              withAttributes:attributes];
}

- (void)mouseDown:(NSEvent *)event {
    self.mouseDownEvent = event;
    self.dragging = NO;
}

- (void)mouseDragged:(NSEvent *)event {
    if (self.dragging || !self.applicationPath) return;
    self.dragging = YES;
    NSURL *fileURL = [NSURL fileURLWithPath:self.applicationPath isDirectory:YES];
    NSDraggingItem *item = [[NSDraggingItem alloc] initWithPasteboardWriter:fileURL];
    NSImage *icon = [[NSWorkspace sharedWorkspace] iconForFile:self.applicationPath];
    [icon setSize:NSMakeSize(48, 48)];
    [item setDraggingFrame:NSMakeRect(15, 15, 48, 48) contents:icon];
    [self beginDraggingSessionWithItems:@[item]
                                 event:self.mouseDownEvent ?: event
                                source:self];
}

- (void)mouseUp:(NSEvent *)event {
    self.mouseDownEvent = nil;
    self.dragging = NO;
}

- (NSDragOperation)draggingSession:(NSDraggingSession *)session
  sourceOperationMaskForDraggingContext:(NSDraggingContext)context {
    return NSDragOperationCopy;
}

- (void)draggingSession:(NSDraggingSession *)session endedAtPoint:(NSPoint)point
              operation:(NSDragOperation)operation {
    self.mouseDownEvent = nil;
    self.dragging = NO;
}

@end

static NSPanel *guidePanel;
static CrabPermissionDragView *dragView;
static NSTextField *instructionLabel;
static BOOL guideScreenRecording;

static BOOL showGuide(NSString *applicationPath, BOOL screenRecording, BOOL reauthorization) {
    @autoreleasepool {
        BOOL isDirectory = NO;
        if (![[NSFileManager defaultManager] fileExistsAtPath:applicationPath
                                                  isDirectory:&isDirectory] || !isDirectory ||
            ![[applicationPath pathExtension] isEqualToString:@"app"]) return NO;

        if (!guidePanel) {
            NSRect frame = NSMakeRect(0, 0, 460, 184);
            guidePanel = [[NSPanel alloc] initWithContentRect:frame
                                                    styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                                                      backing:NSBackingStoreBuffered defer:NO];
            guidePanel.floatingPanel = YES;
            guidePanel.hidesOnDeactivate = NO;
            guidePanel.level = NSFloatingWindowLevel;
            guidePanel.releasedWhenClosed = NO;

            NSView *content = guidePanel.contentView;
            instructionLabel = [NSTextField labelWithString:@""];
            instructionLabel.font = [NSFont systemFontOfSize:13];
            instructionLabel.frame = NSMakeRect(18, 103, 424, 63);
            instructionLabel.maximumNumberOfLines = 3;
            instructionLabel.lineBreakMode = NSLineBreakByWordWrapping;
            [content addSubview:instructionLabel];

            dragView = [[CrabPermissionDragView alloc] initWithFrame:NSMakeRect(18, 17, 424, 76)];
            [content addSubview:dragView];
        }
        guideScreenRecording = screenRecording;
        guidePanel.title = screenRecording ? @"Crab Computer Use · 录屏授权" :
                                             @"Crab Computer Use · 辅助功能授权";
        instructionLabel.stringValue = reauthorization ?
            @"检测到旧版应用已被替换。如列表中已有同名条目，请先选中并点 − 移除，再拖入下方当前应用并打开开关。" : screenRecording ?
            @"将下方应用拖到“录屏与系统录音”的上方列表，然后打开开关" :
            @"将下方应用拖到“辅助功能”列表，然后打开开关";
        dragView.applicationPath = applicationPath;
        [dragView setNeedsDisplay:YES];
        NSScreen *screen = [NSScreen mainScreen] ?: [NSScreen screens].firstObject;
        if (screen) {
            NSRect visible = screen.visibleFrame;
            [guidePanel setFrameOrigin:NSMakePoint(NSMidX(visible) - 230, NSMinY(visible) + 24)];
        }
        [guidePanel orderFrontRegardless];
        [NSApp activateIgnoringOtherApps:YES];
        return guidePanel.visible;
    }
}

bool crab_show_permission_drag_guide(const char *application_path, bool screen_recording,
                                    bool reauthorization) {
    @autoreleasepool {
        if (!application_path) return false;
        NSString *path = [NSString stringWithUTF8String:application_path];
        if (!path) return false;
        if (pthread_main_np()) return showGuide(path, screen_recording, reauthorization);
        __block BOOL visible = NO;
        dispatch_sync(dispatch_get_main_queue(), ^{ visible = showGuide(path, screen_recording, reauthorization); });
        return visible;
    }
}

void crab_hide_permission_drag_guide_if_granted(bool capture_granted, bool accessibility_granted) {
    void (^hideIfGranted)(void) = ^{
        if (guideScreenRecording ? capture_granted : accessibility_granted) {
            [guidePanel orderOut:nil];
        }
    };
    if (pthread_main_np()) {
        hideIfGranted();
    } else {
        dispatch_async(dispatch_get_main_queue(), hideIfGranted);
    }
}
