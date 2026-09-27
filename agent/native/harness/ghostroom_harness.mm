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

extern "C" void ghostroom_harness_update(NSString *json);
extern "C" NSArray<NSString *> *ghostroom_harness_take(void);
extern "C" id ghostroom_harness_controller(void);

@interface NSObject (GRHarness)
- (void)sendMessage;
- (void)stopActive;
- (void)toggleDock;
- (void)togglePanel;
@end

static int failures = 0;

static void Check(BOOL ok, NSString *what)
{
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
    [window drawViewHierarchyInRect:window.bounds afterScreenUpdates:YES];
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
  self.window = [[UIWindow alloc] initWithFrame:[UIScreen mainScreen].bounds];
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
    Check(pill != nil && pill.superview == self.window, @"pill installed in the host window");
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
              Shot(self.window, @"06-keyboard");
              [composer resignFirstResponder];
              CFAbsoluteTime start = CFAbsoluteTimeGetCurrent();
              ghostroom_harness_update(Snapshot(@"long"));
              After(1.0, ^{
                double ms = (CFAbsoluteTimeGetCurrent() - start - 1.0) * 1000.0;
                printf("  long timeline applied (%.0f ms beyond wait)\n", ms);
                Check([table numberOfRowsInSection:0] == [counts[@"long"] integerValue],
                      @"160-item timeline renders");
                Shot(self.window, @"07-long");
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
}

@end

int main(int argc, char *argv[])
{
  setvbuf(stdout, nullptr, _IONBF, 0);
  @autoreleasepool {
    return UIApplicationMain(argc, argv, nil, NSStringFromClass([HarnessDelegate class]));
  }
}
