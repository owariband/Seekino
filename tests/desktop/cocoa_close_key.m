#import <AppKit/AppKit.h>

// Dispatch through the test process's native menu. Never post a system-wide key.
void focusTestWindow(void *view) {
    NSWindow *window = ((__bridge NSView *)view).window;
    [NSApp activateIgnoringOtherApps:YES];
    [window makeKeyWindow];
}

void sendCommandW(void *view) {
    NSWindow *window = ((__bridge NSView *)view).window;
    NSEvent *event = [NSEvent keyEventWithType:NSEventTypeKeyDown location:NSZeroPoint
        modifierFlags:NSEventModifierFlagCommand timestamp:NSProcessInfo.processInfo.systemUptime
        windowNumber:window.windowNumber context:nil characters:@"w" charactersIgnoringModifiers:@"w"
        isARepeat:NO keyCode:13];
    [NSApp.mainMenu performKeyEquivalent:event];
}
