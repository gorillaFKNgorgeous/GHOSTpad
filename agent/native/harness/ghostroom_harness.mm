/* SPDX-License-Identifier: GPL-2.0-or-later
 *
 * iOS Simulator validation harness for the native GHOSTroom workspace.
 *
 * Builds ghostroom_ui.mm without Python (GHOSTROOM_HARNESS), hosts it over a
 * stand-in "Blender" window, feeds it snapshots produced by the real relay and
 * GhostBlender presentation model (make_snapshots.py), drives it like a user
 * and checks the commands it sends back. Screenshots go to Documents/.
 */
#import <UIKit/UIKit.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unistd.h>

extern "C" void ghostroom_harness_update(NSString *json);
extern "C" NSArray<NSString *> *ghostroom_harness_take(void);
extern "C" id ghostroom_harness_controller(void);

/* Stand-in for Blender's GHOSTUIWindow: it consumes hardware key presses and owns a
 * window-level long-press, exactly the behaviours that broke typing and menus when
 * GHOSTroom lived inside Blender's window. */
@interface BlenderLikeWindow : UIWindow
@end

@implementation BlenderLikeWindow
- (void)pressesBegan:(NSSet<UIPress *> *)presses withEvent:(UIPressesEvent *)event
{
}
- (void)pressesEnded:(NSSet<UIPress *> *)presses withEvent:(UIPressesEvent *)event
{
}
@end

@interface NSObject (GRHarness)
- (void)showAgents;
- (void)sendMessage;
- (void)stopActive;
- (void)toggleDock;
- (void)togglePanel;
@end

static int failures = 0;
static const char *volatile lastStep = "launch";

/* A frozen main thread cannot run the harness's own timeout. This background
 * watchdog reports the last completed step and exits so CI shows where it hung. */
static void StartWatchdog(void)
{
  dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
    [NSThread sleepForTimeInterval:75];
    fprintf(stdout, "HARNESS WATCHDOG: main thread stuck after step '%s'\n", lastStep);
    fflush(stdout);
    _exit(4);
  });
}

static void Check(BOOL ok, NSString *what)
{
  lastStep = strdup(what.UTF8String);
  printf("%s %s\n", ok ? "PASS" : "FAIL", what.UTF8String);
  fflush(stdout);
  if (!ok) {
    failures++;
  }
}

static NSString *Snapshot(NSString *name)
{
  NSString *bundle = [NSBundle mainBundle].bundlePath;
  NSString *path = [[bundle stringByAppendingPathComponent:@"snapshots"]
      stringByAppendingPathComponent:[name stringByAppendingString:@".json"]];
  NSString *json = [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:nil];
  return [json stringByReplacingOccurrencesOfString:@"@EVIDENCE@"
                                         withString:[bundle stringByAppendingPathComponent:@"evidence"]];
}

static NSDictionary *Parse(NSString *json)
{
  return [NSJSONSerialization JSONObjectWithData:[json dataUsingEncoding:NSUTF8StringEncoding]
                                         options:0
                                           error:nil];
}

static NSArray<NSDictionary *> *Commands(void)
{
  NSMutableArray *result = [NSMutableArray array];
  for (NSString *json in ghostroom_harness_take()) {
    NSDictionary *command = Parse(json);
    if (command) {
      [result addObject:command];
      printf("  command: %s\n", json.UTF8String);
    }
  }
  fflush(stdout);
  return result;
}

static NSDictionary *Find(NSArray<NSDictionary *> *commands, NSString *type)
{
  for (NSDictionary *command in commands) {
    if ([command[@"type"] isEqual:type]) {
      return command;
    }
  }
  return nil;
}

static void Shot(UIWindow *window, NSString *name)
{
  UIGraphicsImageRenderer *renderer = [[UIGraphicsImageRenderer alloc] initWithBounds:window.bounds];
  UIImage *image = [renderer imageWithActions:^(UIGraphicsImageRendererContext *context) {
    for (UIWindow *each in window.windowScene.windows) {
      if (!each.hidden) {
        [each drawViewHierarchyInRect:window.bounds afterScreenUpdates:YES];
      }
    }
  }];
  NSString *documents = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES).firstObject;
  NSString *path = [documents stringByAppendingPathComponent:[name stringByAppendingString:@".png"]];
  [UIImagePNGRepresentation(image) writeToFile:path atomically:YES];
  printf("  screenshot: %s\n", path.UTF8String);
  fflush(stdout);
}

static void After(double seconds, dispatch_block_t block)
{
  dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(seconds * NSEC_PER_SEC)),
                 dispatch_get_main_queue(),
                 block);
}

@interface HarnessDelegate : UIResponder <UIApplicationDelegate>
@property(nonatomic, strong) UIWindow *window;
@end

@implementation HarnessDelegate

- (BOOL)application:(UIApplication *)application didFinishLaunchingWithOptions:(NSDictionary *)options
{
  self.window = [[BlenderLikeWindow alloc] initWithFrame:[UIScreen mainScreen].bounds];
  [self.window addGestureRecognizer:[[UILongPressGestureRecognizer alloc] initWithTarget:nil action:nil]];
  UIViewController *blender = [[UIViewController alloc] init];
  blender.view.backgroundColor = [UIColor colorWithRed:0.16 green:0.16 blue:0.17 alpha:1];
  UILabel *label = [[UILabel alloc] init];
  label.text = @"Blender viewport (stand-in)";
  label.textColor = [UIColor colorWithWhite:0.6 alpha:1];
  label.translatesAutoresizingMaskIntoConstraints = NO;
  [blender.view addSubview:label];
  [NSLayoutConstraint activateConstraints:@[
    [label.centerXAnchor constraintEqualToAnchor:blender.view.centerXAnchor constant:-200],
    [label.centerYAnchor constraintEqualToAnchor:blender.view.centerYAnchor],
  ]];
  self.window.rootViewController = blender;
  [self.window makeKeyAndVisible];
  After(0.8, ^{
    [self run];
  });
  After(60, ^{
    printf("HARNESS TIMEOUT\n");
    fflush(stdout);
    exit(3);
  });
  return YES;
}

- (void)run
{
  id controller = ghostroom_harness_controller();
  NSDictionary *counts = Parse(Snapshot(@"counts"));

  ghostroom_harness_update(Snapshot(@"empty"));
  After(0.6, ^{
    UIView *pill = [controller valueForKey:@"pill"];
    Check(pill != nil && pill.window != nil && pill.window != self.window,
          @"GHOSTroom lives in its own window, not Blender's");
    Check(pill.window.windowLevel > self.window.windowLevel, @"GHOSTroom window sits above Blender");
    Check([pill.window hitTest:CGPointMake(5, 5) withEvent:nil] == nil,
          @"touches outside GHOSTroom fall through to Blender");
    Check(!pill.hidden, @"pill visible while GHOSTroom is closed");
    Check(Find(Commands(), @"ready") != nil, @"UI announced ready");
    Shot(self.window, @"01-closed-pill");

    /* The runtime asks to open (keyboard shortcut / N-panel button): open_request = 1. */
    ghostroom_harness_update(Snapshot(@"working"));
    After(1.0, ^{
      UIView *panel = [controller valueForKey:@"panel"];
      UITableView *table = [controller valueForKey:@"table"];
      Check(!panel.hidden && panel.alpha > 0.9, @"open_request opens the panel");
      Check([table numberOfRowsInSection:0] == [counts[@"working"] integerValue],
            [NSString stringWithFormat:@"timeline renders every item (%ld)", (long)[counts[@"working"] integerValue]]);
      Check(CGRectGetMaxX(panel.frame) <= CGRectGetMaxX(self.window.bounds) &&
                panel.frame.size.width < self.window.bounds.size.width * 0.8,
            @"panel leaves Blender visible beside it");
      UILabel *activity = [controller valueForKey:@"activityLabel"];
      Check([activity.text containsString:@"Working in Blender"], @"activity bar shows live state");
      UIButton *stop = [controller valueForKey:@"activityStop"];
      Check(!stop.hidden, @"stop is offered while the agent works");
      NSDictionary *visible = Find(Commands(), @"visible");
      Check([visible[@"open"] boolValue], @"UI reported it is open");
      Shot(self.window, @"02-working");

      UITextView *composer = [controller valueForKey:@"composer"];
      composer.text = @"Make the wheels a bit bigger\nand keep the arches aligned";
      [controller sendMessage];
      NSArray *commands = Commands();
      NSDictionary *send = Find(commands, @"send");
      Check([send[@"text"] containsString:@"\n"], @"multiline message sent intact");
      Check([send[@"mode"] isEqual:@"do"] && [send[@"agent_id"] isEqual:@"codex"],
            @"send carries the selected agent and mode");
      Check([send[@"id"] length] == 32, @"message id is a 32-hex idempotency key");
      Check(composer.text.length == 0, @"composer cleared after send");

      [controller stopActive];
      NSDictionary *stopCommand = Find(Commands(), @"stop");
      NSDictionary *working = Parse(Snapshot(@"working"));
      Check([stopCommand[@"task_id"] isEqual:working[@"activity"][@"task_id"]], @"stop targets the active task");

      UIButton *attach = [controller valueForKey:@"attachButton"];
      Check(attach.menu.children.count == 2, @"attach menu offers Blender and iPad context");

      ghostroom_harness_update(Snapshot(@"done"));
      After(0.8, ^{
        Check([table numberOfRowsInSection:0] == [counts[@"done"] integerValue], @"agent reply appended");
        Shot(self.window, @"03-done");
        ghostroom_harness_update(Snapshot(@"recovery"));
        After(0.8, ^{
          UIStackView *recovery = [controller valueForKey:@"recoveryBox"];
          Check(recovery.arrangedSubviews.count == 1, @"recovery card shown after interruption");
          Shot(self.window, @"04-recovery");
          [controller toggleDock];
          After(0.8, ^{
            Check(panel.frame.origin.x < self.window.bounds.size.width / 2, @"panel docks to the left");
            Shot(self.window, @"05-docked-left");
            [controller toggleDock];
            [composer becomeFirstResponder];
            After(1.2, ^{
              Check(composer.isFirstResponder, @"composer takes keyboard focus");
              Check(composer.window.isKeyWindow, @"GHOSTroom window is key while typing");
              [composer insertText:@"typed"];
              Check([composer.text hasSuffix:@"typed"], @"text input reaches the composer");
              /* A docked software keyboard covering the lower 45% of the screen. */
              CGRect screen = composer.window.screen.bounds;
              CGRect keyboard = CGRectMake(0, screen.size.height * 0.55, screen.size.width, screen.size.height * 0.45);
              [[NSNotificationCenter defaultCenter]
                  postNotificationName:UIKeyboardWillChangeFrameNotification
                                object:nil
                              userInfo:@{UIKeyboardFrameEndUserInfoKey : [NSValue valueWithCGRect:keyboard],
                                         UIKeyboardAnimationDurationUserInfoKey : @0}];
              [composer.window layoutIfNeeded];
              CGRect box = [composer convertRect:composer.bounds toView:nil];
              Check(CGRectGetMaxY(box) <= CGRectGetMinY(keyboard) && CGRectGetMinY(box) >= 0,
                    @"composer stays visible above a docked keyboard");
              Shot(self.window, @"06-keyboard");
              [composer resignFirstResponder];
              [[NSNotificationCenter defaultCenter] postNotificationName:UIKeyboardWillHideNotification
                                                                  object:nil
                                                                userInfo:@{UIKeyboardAnimationDurationUserInfoKey : @0}];
              Check(self.window.isKeyWindow, @"keyboard returns to Blender after typing");
              composer.text = @"";
              CFAbsoluteTime start = CFAbsoluteTimeGetCurrent();
              ghostroom_harness_update(Snapshot(@"long"));
              After(1.0, ^{
                double ms = (CFAbsoluteTimeGetCurrent() - start - 1.0) * 1000.0;
                printf("  long timeline applied (%.0f ms beyond wait)\n", ms);
                Check([table numberOfRowsInSection:0] == [counts[@"long"] integerValue],
                      @"160-item timeline renders");
                Shot(self.window, @"07-long");
                [controller showAgents];
                After(1.0, ^{
                UIViewController *presented = composer.window.rootViewController.presentedViewController;
                Check([presented isKindOfClass:[UINavigationController class]], @"agents sheet opens");
                UITableViewController *sheet = (UITableViewController *)
                    ((UINavigationController *)presented).viewControllers.firstObject;
                Check([sheet.tableView numberOfRowsInSection:0] >= 1 &&
                          [sheet.tableView numberOfRowsInSection:1] >= 1,
                      @"agents sheet lists agents and connectors");
                Shot(self.window, @"08-agents");
                Shot(composer.window, @"08b-agents-overlay");
                [presented dismissViewControllerAnimated:NO completion:nil];
                [controller togglePanel];
                After(0.8, ^{
                  UIView *pill = [controller valueForKey:@"pill"];
                  UILabel *pillLabel = [controller valueForKey:@"pillLabel"];
                  Check(!pill.hidden && panel.hidden, @"minimise returns the screen to Blender");
                  Check([pillLabel.text containsString:@"Codex"], @"pill keeps showing live activity");
                  Shot(self.window, @"08-minimised");
                  printf("HARNESS RESULT failures=%d\n", failures);
                  fflush(stdout);
                  exit(failures ? 1 : 0);
                });
                });
              });
            });
          });
        });
      });
    });
  });
}

@end

int main(int argc, char *argv[])
{
  setvbuf(stdout, nullptr, _IONBF, 0);
  StartWatchdog();
  @autoreleasepool {
    return UIApplicationMain(argc, argv, nil, NSStringFromClass([HarnessDelegate class]));
  }
}
