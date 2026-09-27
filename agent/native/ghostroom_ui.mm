/* SPDX-License-Identifier: GPL-2.0-or-later
 *
 * GHOSTroom: the native iPadOS AI workspace inside GHOSTpad.
 *
 * Responsibilities (target.md §3, §4, §23): native presentation and input only.
 * Text composition, keyboard, selection/copy, scrolling, touch/Pencil controls,
 * media and file picking, rich activity/evidence presentation and navigation.
 *
 * It never talks to an agent or to Blender directly. It renders the JSON
 * snapshot built by GhostBlender's Python runtime (ghostroom.py) and hands
 * small JSON commands back to it. Scene manipulation stays on the one
 * authoritative path: agent -> GhostBlender tool layer -> Blender main thread.
 *
 * The overlay is added to Blender's own UIWindow above the Metal view, as a
 * sibling of Blender's view: Blender's gesture recognizers never see touches
 * that land on GHOSTroom, and every touch outside it still reaches Blender.
 */
#ifndef GHOSTROOM_HARNESS
#  define PY_SSIZE_T_CLEAN
#  include <Python.h>
#endif
#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>
#import <PhotosUI/PhotosUI.h>
#import <UniformTypeIdentifiers/UniformTypeIdentifiers.h>

/* ------------------------------------------------------------------------ helpers */

static NSString *GRStr(id value)
{
  if ([value isKindOfClass:[NSString class]]) {
    return value;
  }
  if ([value isKindOfClass:[NSNumber class]]) {
    return [value stringValue];
  }
  return @"";
}

static double GRNum(id value)
{
  return [value isKindOfClass:[NSNumber class]] ? [value doubleValue] : 0.0;
}

static BOOL GRBool(id value)
{
  return [value isKindOfClass:[NSNumber class]] ? [value boolValue] : NO;
}

static NSArray *GRArr(id value)
{
  return [value isKindOfClass:[NSArray class]] ? value : @[];
}

static NSDictionary *GRDict(id value)
{
  return [value isKindOfClass:[NSDictionary class]] ? value : nil;
}

static UIColor *GRHex(uint32_t rgb, CGFloat alpha)
{
  return [UIColor colorWithRed:((rgb >> 16) & 0xFF) / 255.0
                         green:((rgb >> 8) & 0xFF) / 255.0
                          blue:(rgb & 0xFF) / 255.0
                         alpha:alpha];
}

/* GHOSTpad visual language: graphite glass, ghost-mint signal, spectral violet for
 * collaboration, warm amber for caution and coral for failure. */
static UIColor *GRMint(void) { return GRHex(0x6FE3CF, 1.0); }
static UIColor *GRViolet(void) { return GRHex(0xA99BFF, 1.0); }
static UIColor *GRAmber(void) { return GRHex(0xF5B95A, 1.0); }
static UIColor *GRCoral(void) { return GRHex(0xFF7A70, 1.0); }
static UIColor *GRInk(void) { return GRHex(0xEEF2F8, 1.0); }
static UIColor *GRMuted(void) { return GRHex(0x9AA3B5, 1.0); }
static UIColor *GRSurface(void) { return GRHex(0x1B1F2A, 0.92); }
static UIColor *GRCard(void) { return GRHex(0x252B39, 0.95); }

static UIFont *GRFont(CGFloat size, UIFontWeight weight)
{
  return [UIFont systemFontOfSize:size weight:weight];
}

static UIFont *GRRounded(CGFloat size, UIFontWeight weight)
{
  UIFont *font = [UIFont systemFontOfSize:size weight:weight];
  UIFontDescriptor *descriptor = [font.fontDescriptor
      fontDescriptorWithDesign:UIFontDescriptorSystemDesignRounded];
  return descriptor ? [UIFont fontWithDescriptor:descriptor size:size] : font;
}

static UIImage *GRSymbol(NSString *name, CGFloat size, UIFontWeight weight)
{
  UIImageSymbolConfiguration *config =
      [UIImageSymbolConfiguration configurationWithPointSize:size
                                                      weight:(UIImageSymbolWeight)(
                                                                 weight >= UIFontWeightBold ?
                                                                     UIImageSymbolWeightBold :
                                                                     UIImageSymbolWeightMedium)];
  return [UIImage systemImageNamed:name withConfiguration:config];
}

static NSString *GRClock(double epoch)
{
  if (epoch <= 0) {
    return @"";
  }
  static NSDateFormatter *formatter;
  if (!formatter) {
    formatter = [[NSDateFormatter alloc] init];
    formatter.timeStyle = NSDateFormatterShortStyle;
    formatter.dateStyle = NSDateFormatterNoStyle;
  }
  return [formatter stringFromDate:[NSDate dateWithTimeIntervalSince1970:epoch]];
}

static NSString *GRElapsed(double since)
{
  if (since <= 0) {
    return @"";
  }
  long seconds = (long)MAX(0.0, [[NSDate date] timeIntervalSince1970] - since);
  if (seconds < 60) {
    return [NSString stringWithFormat:@"%lds", seconds];
  }
  if (seconds < 3600) {
    return [NSString stringWithFormat:@"%ldm %02lds", seconds / 60, seconds % 60];
  }
  return [NSString stringWithFormat:@"%ldh %02ldm", seconds / 3600, (seconds % 3600) / 60];
}

static UIColor *GRStateColor(NSString *state)
{
  if ([state isEqualToString:@"failed"] || [state isEqualToString:@"uncertain"]) {
    return GRCoral();
  }
  if ([state isEqualToString:@"stopping"] || [state isEqualToString:@"stopped"] ||
      [state isEqualToString:@"waiting"] || [state isEqualToString:@"needs_input"] ||
      [state isEqualToString:@"queued"] || [state isEqualToString:@"cancelled"])
  {
    return GRAmber();
  }
  if ([state isEqualToString:@"completed"] || [state isEqualToString:@"done"] ||
      [state isEqualToString:@"idle"])
  {
    return GRMuted();
  }
  if ([state isEqualToString:@"reviewing"] || [state isEqualToString:@"inspecting"]) {
    return GRViolet();
  }
  return GRMint();
}

static NSString *GRStateSymbol(NSString *state)
{
  NSDictionary *symbols = @{
    @"inspecting" : @"eye",
    @"executing" : @"hammer",
    @"reviewing" : @"photo.on.rectangle",
    @"planning" : @"list.bullet.rectangle",
    @"thinking" : @"ellipsis.bubble",
    @"waiting" : @"hourglass",
    @"queued" : @"clock",
    @"stopping" : @"stop.circle",
    @"stopped" : @"stop.circle",
    @"failed" : @"exclamationmark.triangle",
    @"needs_input" : @"questionmark.bubble",
    @"sending" : @"paperplane",
    @"idle" : @"sparkles",
    @"running" : @"bolt",
    @"completed" : @"checkmark.circle",
    @"uncertain" : @"exclamationmark.triangle",
  };
  return symbols[state] ?: @"sparkles";
}

static NSString *GRTaskStateLabel(NSString *state)
{
  NSDictionary *labels = @{
    @"queued" : @"Queued",
    @"running" : @"Working",
    @"stopping" : @"Stopping",
    @"stopped" : @"Stopped",
    @"completed" : @"Done",
    @"failed" : @"Failed",
    @"uncertain" : @"Interrupted",
  };
  return labels[state] ?: state.capitalizedString;
}

/* Inline Markdown from agents (bold, italic, code), keeping the text selectable. */
static NSAttributedString *GRMarkdown(NSString *text, CGFloat size, UIColor *color)
{
  NSDictionary *base = @{NSFontAttributeName : GRFont(size, UIFontWeightRegular),
                         NSForegroundColorAttributeName : color};
  NSAttributedStringMarkdownParsingOptions *options =
      [[NSAttributedStringMarkdownParsingOptions alloc] init];
  options.interpretedSyntax = NSAttributedStringMarkdownInterpretedSyntaxInlineOnlyPreservingWhitespace;
  options.failurePolicy = NSAttributedStringMarkdownParsingFailureReturnPartiallyParsedIfPossible;
  NSError *error = nil;
  NSAttributedString *parsed = [[NSAttributedString alloc] initWithMarkdownString:text
                                                                          options:options
                                                                          baseURL:nil
                                                                            error:&error];
  if (!parsed) {
    return [[NSAttributedString alloc] initWithString:text attributes:base];
  }
  NSMutableAttributedString *result = [parsed mutableCopy];
  NSRange all = NSMakeRange(0, result.length);
  [result addAttributes:base range:all];
  [parsed enumerateAttribute:NSInlinePresentationIntentAttributeName
                     inRange:all
                     options:0
                  usingBlock:^(id value, NSRange range, BOOL *stop) {
                    NSUInteger intent = [value isKindOfClass:[NSNumber class]] ?
                                            [value unsignedIntegerValue] :
                                            0;
                    if (intent & NSInlinePresentationIntentCode) {
                      [result addAttribute:NSFontAttributeName
                                     value:[UIFont monospacedSystemFontOfSize:size - 1
                                                                       weight:UIFontWeightRegular]
                                     range:range];
                      [result addAttribute:NSForegroundColorAttributeName value:GRMint() range:range];
                    }
                    else if (intent & NSInlinePresentationIntentStronglyEmphasized) {
                      [result addAttribute:NSFontAttributeName
                                     value:GRFont(size, UIFontWeightSemibold)
                                     range:range];
                    }
                    else if (intent & NSInlinePresentationIntentEmphasized) {
                      [result addAttribute:NSFontAttributeName
                                     value:[UIFont italicSystemFontOfSize:size]
                                     range:range];
                    }
                  }];
  return result;
}

static UILabel *GRLabel(CGFloat size, UIFontWeight weight, UIColor *color, NSInteger lines)
{
  UILabel *label = [[UILabel alloc] init];
  label.font = GRFont(size, weight);
  label.textColor = color;
  label.numberOfLines = lines;
  return label;
}

static UIButton *GRPlainButton(NSString *title, NSString *symbol, UIColor *tint)
{
  UIButtonConfiguration *config = [UIButtonConfiguration plainButtonConfiguration];
  config.title = title;
  config.image = symbol ? GRSymbol(symbol, 13, UIFontWeightSemibold) : nil;
  config.imagePadding = 5;
  config.baseForegroundColor = tint;
  config.contentInsets = NSDirectionalEdgeInsetsMake(6, 8, 6, 8);
  config.titleTextAttributesTransformer = ^NSDictionary *(NSDictionary *attributes) {
    NSMutableDictionary *copy = [attributes mutableCopy];
    copy[NSFontAttributeName] = GRFont(14, UIFontWeightSemibold);
    return copy;
  };
  UIButton *button = [UIButton buttonWithConfiguration:config primaryAction:nil];
  button.pointerInteractionEnabled = YES;
  return button;
}

static UIButton *GRTintedButton(NSString *title, NSString *symbol, UIColor *tint)
{
  UIButtonConfiguration *config = [UIButtonConfiguration tintedButtonConfiguration];
  config.title = title;
  config.image = symbol ? GRSymbol(symbol, 13, UIFontWeightSemibold) : nil;
  config.imagePadding = 5;
  config.baseForegroundColor = tint;
  config.baseBackgroundColor = tint;
  config.cornerStyle = UIButtonConfigurationCornerStyleCapsule;
  config.contentInsets = NSDirectionalEdgeInsetsMake(8, 14, 8, 14);
  config.titleTextAttributesTransformer = ^NSDictionary *(NSDictionary *attributes) {
    NSMutableDictionary *copy = [attributes mutableCopy];
    copy[NSFontAttributeName] = GRFont(14, UIFontWeightSemibold);
    return copy;
  };
  UIButton *button = [UIButton buttonWithConfiguration:config primaryAction:nil];
  button.pointerInteractionEnabled = YES;
  return button;
}

static UIView *GRSpacer(void)
{
  UIView *view = [[UIView alloc] init];
  [view setContentHuggingPriority:1 forAxis:UILayoutConstraintAxisHorizontal];
  [view setContentCompressionResistancePriority:1 forAxis:UILayoutConstraintAxisHorizontal];
  return view;
}

static UIStackView *GRStack(NSArray *views, UILayoutConstraintAxis axis, CGFloat spacing)
{
  UIStackView *stack = [[UIStackView alloc] initWithArrangedSubviews:views];
  stack.axis = axis;
  stack.spacing = spacing;
  stack.alignment = axis == UILayoutConstraintAxisHorizontal ? UIStackViewAlignmentCenter :
                                                               UIStackViewAlignmentFill;
  return stack;
}

/* Selectable, copyable, non-scrolling text (native selection, copy, look up). */
static UITextView *GRTextBody(void)
{
  UITextView *text = [[UITextView alloc] init];
  text.editable = NO;
  text.selectable = YES;
  text.scrollEnabled = NO;
  text.backgroundColor = [UIColor clearColor];
  text.textContainerInset = UIEdgeInsetsZero;
  text.textContainer.lineFragmentPadding = 0;
  text.dataDetectorTypes = UIDataDetectorTypeLink;
  text.linkTextAttributes = @{NSForegroundColorAttributeName : GRMint()};
  return text;
}

/* ------------------------------------------------------------------------ commands */

static NSMutableArray<NSString *> *GROutbox(void)
{
  static NSMutableArray<NSString *> *outbox;
  static dispatch_once_t once;
  dispatch_once(&once, ^{
    outbox = [NSMutableArray array];
  });
  return outbox;
}

static void GRSend(NSDictionary *command)
{
  NSData *data = [NSJSONSerialization dataWithJSONObject:command options:0 error:nil];
  if (!data) {
    return;
  }
  NSString *json = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
  NSMutableArray *outbox = GROutbox();
  @synchronized(outbox) {
    if (outbox.count < 256) {
      [outbox addObject:json];
    }
  }
}

static NSString *GRNewId(void)
{
  return [[[NSUUID UUID].UUIDString stringByReplacingOccurrencesOfString:@"-" withString:@""]
      lowercaseString];
}

/* ------------------------------------------------------------------------ image cache */

static NSCache<NSString *, UIImage *> *GRImages(void)
{
  static NSCache *cache;
  static dispatch_once_t once;
  dispatch_once(&once, ^{
    cache = [[NSCache alloc] init];
    cache.countLimit = 120;
  });
  return cache;
}

static UIImage *GRImageAt(NSString *path)
{
  if (path.length == 0) {
    return nil;
  }
  UIImage *image = [GRImages() objectForKey:path];
  if (!image) {
    image = [UIImage imageWithContentsOfFile:path];
    if (image) {
      [GRImages() setObject:image forKey:path];
    }
  }
  return image;
}

/* ------------------------------------------------------------------------ image viewer */

@interface GRImageViewer : UIViewController <UIScrollViewDelegate>
@property(nonatomic, strong) UIImage *image;
@property(nonatomic, copy) NSString *caption;
@property(nonatomic, strong) UIScrollView *scroll;
@property(nonatomic, strong) UIImageView *imageView;
@end

@implementation GRImageViewer
- (void)viewDidLoad
{
  [super viewDidLoad];
  self.view.backgroundColor = [UIColor colorWithWhite:0.04 alpha:0.97];
  self.scroll = [[UIScrollView alloc] init];
  self.scroll.delegate = self;
  self.scroll.minimumZoomScale = 1.0;
  self.scroll.maximumZoomScale = 6.0;
  self.scroll.translatesAutoresizingMaskIntoConstraints = NO;
  [self.view addSubview:self.scroll];
  self.imageView = [[UIImageView alloc] initWithImage:self.image];
  self.imageView.contentMode = UIViewContentModeScaleAspectFit;
  self.imageView.translatesAutoresizingMaskIntoConstraints = NO;
  [self.scroll addSubview:self.imageView];

  UILabel *caption = GRLabel(14, UIFontWeightMedium, GRInk(), 2);
  caption.text = self.caption;
  UIButton *share = GRTintedButton(@"Share", @"square.and.arrow.up", GRMint());
  [share addTarget:self action:@selector(share:) forControlEvents:UIControlEventPrimaryActionTriggered];
  UIButton *close = GRTintedButton(@"Close", @"xmark", GRInk());
  [close addTarget:self action:@selector(close) forControlEvents:UIControlEventPrimaryActionTriggered];
  UIStackView *bar = GRStack(@[ caption, GRSpacer(), share, close ], UILayoutConstraintAxisHorizontal, 10);
  bar.translatesAutoresizingMaskIntoConstraints = NO;
  [self.view addSubview:bar];

  UILayoutGuide *safe = self.view.safeAreaLayoutGuide;
  [NSLayoutConstraint activateConstraints:@[
    [bar.leadingAnchor constraintEqualToAnchor:safe.leadingAnchor constant:20],
    [bar.trailingAnchor constraintEqualToAnchor:safe.trailingAnchor constant:-20],
    [bar.topAnchor constraintEqualToAnchor:safe.topAnchor constant:12],
    [self.scroll.topAnchor constraintEqualToAnchor:bar.bottomAnchor constant:12],
    [self.scroll.leadingAnchor constraintEqualToAnchor:safe.leadingAnchor],
    [self.scroll.trailingAnchor constraintEqualToAnchor:safe.trailingAnchor],
    [self.scroll.bottomAnchor constraintEqualToAnchor:safe.bottomAnchor],
    [self.imageView.leadingAnchor constraintEqualToAnchor:self.scroll.contentLayoutGuide.leadingAnchor],
    [self.imageView.trailingAnchor constraintEqualToAnchor:self.scroll.contentLayoutGuide.trailingAnchor],
    [self.imageView.topAnchor constraintEqualToAnchor:self.scroll.contentLayoutGuide.topAnchor],
    [self.imageView.bottomAnchor constraintEqualToAnchor:self.scroll.contentLayoutGuide.bottomAnchor],
    [self.imageView.widthAnchor constraintEqualToAnchor:self.scroll.frameLayoutGuide.widthAnchor],
    [self.imageView.heightAnchor constraintEqualToAnchor:self.scroll.frameLayoutGuide.heightAnchor],
  ]];
  UITapGestureRecognizer *doubleTap = [[UITapGestureRecognizer alloc] initWithTarget:self
                                                                               action:@selector(zoom:)];
  doubleTap.numberOfTapsRequired = 2;
  [self.scroll addGestureRecognizer:doubleTap];
}
- (UIView *)viewForZoomingInScrollView:(UIScrollView *)scrollView
{
  return self.imageView;
}
- (void)zoom:(UITapGestureRecognizer *)gesture
{
  [self.scroll setZoomScale:(self.scroll.zoomScale > 1.01 ? 1.0 : 2.5) animated:YES];
}
- (void)share:(UIButton *)sender
{
  if (!self.image) {
    return;
  }
  UIActivityViewController *activity = [[UIActivityViewController alloc]
      initWithActivityItems:@[ self.image ]
      applicationActivities:nil];
  activity.popoverPresentationController.sourceView = sender;
  [self presentViewController:activity animated:YES completion:nil];
}
- (void)close
{
  [self dismissViewControllerAnimated:YES completion:nil];
}
@end

/* ------------------------------------------------------------------------ composer */

@interface GRTextView : UITextView
@property(nonatomic, copy) void (^onSubmit)(void);
@end

@implementation GRTextView
- (NSArray<UIKeyCommand *> *)keyCommands
{
  /* Return inserts a newline (multiline composition); Command-Return sends. */
  UIKeyCommand *send = [UIKeyCommand commandWithTitle:@"Send to agent"
                                                image:nil
                                               action:@selector(submitFromKeyboard:)
                                                input:@"\r"
                                        modifierFlags:UIKeyModifierCommand
                                         propertyList:nil];
  send.wantsPriorityOverSystemBehavior = YES;
  UIKeyCommand *dismiss = [UIKeyCommand keyCommandWithInput:UIKeyInputEscape
                                              modifierFlags:0
                                                     action:@selector(dismissFromKeyboard:)];
  return @[ send, dismiss ];
}
- (void)submitFromKeyboard:(id)sender
{
  if (self.onSubmit) {
    self.onSubmit();
  }
}
- (void)dismissFromKeyboard:(id)sender
{
  [self resignFirstResponder];
}
@end

@class GRController;

/* GHOSTroom's own window, above Blender's. Blender's GHOSTUIWindow consumes hardware
 * key presses (pressesBegan) and owns window-level gestures, so GHOSTroom must not
 * live inside it: here text input and menus get a normal UIKit responder chain.
 * Touches that land on no GHOSTroom view fall through to Blender's window below. */
@interface GROverlayWindow : UIWindow
@end

@implementation GROverlayWindow
- (UIView *)hitTest:(CGPoint)point withEvent:(UIEvent *)event
{
  UIView *hit = [super hitTest:point withEvent:event];
  if (hit == self || hit == self.rootViewController.view) {
    return nil;
  }
  return hit;
}
@end

@interface GROverlayRoot : UIViewController
@end

@implementation GROverlayRoot
- (void)loadView
{
  UIView *view = [[UIView alloc] init];
  view.backgroundColor = [UIColor clearColor];
  self.view = view;
}
- (BOOL)prefersStatusBarHidden
{
  return YES;
}
- (UIInterfaceOrientationMask)supportedInterfaceOrientations
{
  return UIInterfaceOrientationMaskAll;
}
@end

@interface GRController : NSObject <UITableViewDataSource,
                                    UITableViewDelegate,
                                    UITextViewDelegate,
                                    PHPickerViewControllerDelegate,
                                    UIDocumentPickerDelegate,
                                    UIGestureRecognizerDelegate>
+ (instancetype)shared;
- (void)applySnapshot:(NSString *)json;
@end


/* ------------------------------------------------------------------------ agents & connections */

/* Sign agents in, add or remove API keys and create personal connectors for external
 * agents (ChatGPT, Claude, ...) without redeploying the relay. Credentials go straight
 * to the relay through GhostBlender's authenticated exchange and are never shown again. */
@interface GRAgentsSheet : UITableViewController
@property(nonatomic, strong) NSDictionary *setup;
- (void)refresh:(NSDictionary *)setup;
@end

@implementation GRAgentsSheet

- (instancetype)init
{
  return [super initWithStyle:UITableViewStyleInsetGrouped];
}

- (void)viewDidLoad
{
  [super viewDidLoad];
  self.title = @"Agents & connections";
  self.overrideUserInterfaceStyle = UIUserInterfaceStyleDark;
  self.navigationItem.rightBarButtonItem = [[UIBarButtonItem alloc]
      initWithBarButtonSystemItem:UIBarButtonSystemItemDone
                           target:self
                           action:@selector(done)];
  self.tableView.rowHeight = UITableViewAutomaticDimension;
  self.tableView.estimatedRowHeight = 64;
}

- (void)done
{
  [self dismissViewControllerAnimated:YES completion:nil];
}

- (void)refresh:(NSDictionary *)setup
{
  if ([setup isEqual:self.setup]) {
    return;
  }
  self.setup = setup;
  if (self.isViewLoaded) {
    [self.tableView reloadData];
  }
}

- (NSArray *)agents
{
  NSMutableArray *rows = [NSMutableArray array];
  for (NSDictionary *agent in GRArr(self.setup[@"agents"])) {
    if (!GRBool(agent[@"external"])) {
      [rows addObject:agent];
    }
  }
  return rows;
}

- (NSArray *)connectors
{
  return GRArr(self.setup[@"connectors"]);
}

- (NSInteger)numberOfSectionsInTableView:(UITableView *)tableView
{
  return 2;
}

- (NSString *)tableView:(UITableView *)tableView titleForHeaderInSection:(NSInteger)section
{
  return section == 0 ? @"Agents run by the relay" : @"External agents (connectors)";
}

- (NSString *)tableView:(UITableView *)tableView titleForFooterInSection:(NSInteger)section
{
  if (section == 0) {
    return @"Sign-in details and keys are stored only on your relay.";
  }
  NSDictionary *result = GRDict(self.setup[@"connector_result"]);
  NSString *base = @"A connector URL lets ChatGPT, Claude or another MCP client join this GHOSTroom as its own "
                   @"participant with full access. Its URL is shown once — copy it into the client's "
                   @"connector settings.";
  return result ? [NSString stringWithFormat:@"%@\n\n%@", GRStr(result[@"message"]), base] : base;
}

- (NSInteger)tableView:(UITableView *)tableView numberOfRowsInSection:(NSInteger)section
{
  return section == 0 ? (NSInteger)[self agents].count : (NSInteger)[self connectors].count + 1;
}

- (UITableViewCell *)tableView:(UITableView *)tableView cellForRowAtIndexPath:(NSIndexPath *)indexPath
{
  UITableViewCell *cell = [[UITableViewCell alloc] initWithStyle:UITableViewCellStyleSubtitle
                                                 reuseIdentifier:nil];
  UIListContentConfiguration *content = [UIListContentConfiguration subtitleCellConfiguration];
  content.secondaryTextProperties.numberOfLines = 0;
  content.secondaryTextProperties.color = GRMuted();
  if (indexPath.section == 0) {
    NSDictionary *agent = [self agents][(NSUInteger)indexPath.row];
    NSString *state = GRStr(agent[@"state"]);
    NSMutableArray *lines = [NSMutableArray arrayWithObject:[NSString stringWithFormat:@"%@ · %@",
                                                             state.capitalizedString,
                                                             [GRStr(agent[@"auth"]) stringByReplacingOccurrencesOfString:@"_"
                                                                                                           withString:@" "]]];
    if (GRStr(agent[@"message"]).length) {
      [lines addObject:GRStr(agent[@"message"])];
    }
    NSDictionary *result = GRDict(agent[@"result"]);
    if (result && GRStr(result[@"message"]).length) {
      [lines addObject:GRStr(result[@"message"])];
    }
    if (GRStr(agent[@"code"]).length) {
      [lines addObject:[NSString stringWithFormat:@"Code: %@ — tap to open the sign-in page (code is copied)",
                                                  GRStr(agent[@"code"])]];
    }
    content.text = GRStr(agent[@"name"]);
    content.secondaryText = [lines componentsJoinedByString:@"\n"];
    UIColor *dot = [state isEqualToString:@"available"] ? GRMint() :
                   ([state isEqualToString:@"unavailable"] ? GRCoral() : GRAmber());
    content.image = [GRSymbol(@"circle.fill", 11, UIFontWeightBold) imageWithTintColor:dot
                                                                         renderingMode:UIImageRenderingModeAlwaysOriginal];
    cell.accessoryType = GRArr(agent[@"methods"]).count ? UITableViewCellAccessoryDisclosureIndicator :
                                                         UITableViewCellAccessoryNone;
  }
  else if ((NSUInteger)indexPath.row < [self connectors].count) {
    NSDictionary *connector = [self connectors][(NSUInteger)indexPath.row];
    BOOL shared = GRBool(connector[@"shared"]);
    content.text = shared ? @"GhostBlender Simple (shared URL)" : GRStr(connector[@"name"]);
    NSMutableArray *lines = [NSMutableArray array];
    if (GRStr(connector[@"client"]).length) {
      [lines addObject:[@"Client: " stringByAppendingString:GRStr(connector[@"client"])]];
    }
    double seen = GRNum(connector[@"last_seen"]);
    [lines addObject:seen > 0 ? [NSString stringWithFormat:@"Last active %@", GRClock(seen)] : @"Not used yet"];
    if (GRStr(connector[@"url"]).length) {
      [lines addObject:[@"Tap to copy: " stringByAppendingString:GRStr(connector[@"url"])]];
    }
    else if (shared) {
      [lines addObject:@"Configured on the relay; cannot be revoked here"];
    }
    content.secondaryText = [lines componentsJoinedByString:@"\n"];
    content.image = GRSymbol(@"link", 15, UIFontWeightMedium);
  }
  else {
    content.text = @"Connect an external agent…";
    content.textProperties.color = GRMint();
    content.image = GRSymbol(@"plus.circle.fill", 17, UIFontWeightSemibold);
  }
  cell.contentConfiguration = content;
  return cell;
}

- (void)tableView:(UITableView *)tableView didSelectRowAtIndexPath:(NSIndexPath *)indexPath
{
  [tableView deselectRowAtIndexPath:indexPath animated:YES];
  UIView *source = [tableView cellForRowAtIndexPath:indexPath] ?: tableView;
  if (indexPath.section == 0) {
    [self agentActions:[self agents][(NSUInteger)indexPath.row] source:source];
  }
  else if ((NSUInteger)indexPath.row < [self connectors].count) {
    [self connectorActions:[self connectors][(NSUInteger)indexPath.row] source:source];
  }
  else {
    [self createConnector];
  }
}

- (void)agentActions:(NSDictionary *)agent source:(UIView *)source
{
  NSString *identifier = GRStr(agent[@"id"]);
  NSString *code = GRStr(agent[@"code"]);
  NSURL *url = GRStr(agent[@"url"]).length ? [NSURL URLWithString:GRStr(agent[@"url"])] : nil;
  if (code.length && url) {
    [UIPasteboard generalPasteboard].string = code;
    [[UIApplication sharedApplication] openURL:url options:@{} completionHandler:nil];
    return;
  }
  NSArray *methods = GRArr(agent[@"methods"]);
  if (!methods.count) {
    return;
  }
  UIAlertController *sheet = [UIAlertController alertControllerWithTitle:GRStr(agent[@"name"])
                                                                 message:GRStr(agent[@"message"])
                                                          preferredStyle:UIAlertControllerStyleActionSheet];
  if ([methods containsObject:@"sign_in"]) {
    [sheet addAction:[UIAlertAction actionWithTitle:@"Sign in"
                                              style:UIAlertActionStyleDefault
                                            handler:^(UIAlertAction *a) {
                                              GRSend(@{@"type" : @"agent_setup", @"agent_id" : identifier,
                                                       @"op" : @"sign_in"});
                                            }]];
  }
  if ([methods containsObject:@"api_key"]) {
    [sheet addAction:[UIAlertAction actionWithTitle:@"Set API key…"
                                              style:UIAlertActionStyleDefault
                                            handler:^(UIAlertAction *a) {
                                              [self askForKey:identifier name:GRStr(agent[@"name"])];
                                            }]];
  }
  if ([methods containsObject:@"sign_out"]) {
    [sheet addAction:[UIAlertAction actionWithTitle:@"Sign out"
                                              style:UIAlertActionStyleDestructive
                                            handler:^(UIAlertAction *a) {
                                              GRSend(@{@"type" : @"agent_setup", @"agent_id" : identifier,
                                                       @"op" : @"sign_out"});
                                            }]];
  }
  [sheet addAction:[UIAlertAction actionWithTitle:@"Cancel" style:UIAlertActionStyleCancel handler:nil]];
  sheet.popoverPresentationController.sourceView = source;
  sheet.popoverPresentationController.sourceRect = source.bounds;
  [self presentViewController:sheet animated:YES completion:nil];
}

- (void)askForKey:(NSString *)identifier name:(NSString *)name
{
  UIAlertController *alert = [UIAlertController
      alertControllerWithTitle:[NSString stringWithFormat:@"%@ API key", name]
                       message:@"Stored only on your relay. It is never shown again."
                preferredStyle:UIAlertControllerStyleAlert];
  [alert addTextFieldWithConfigurationHandler:^(UITextField *field) {
    field.secureTextEntry = YES;
    field.placeholder = @"API key";
    field.autocorrectionType = UITextAutocorrectionTypeNo;
    field.autocapitalizationType = UITextAutocapitalizationTypeNone;
  }];
  __weak UIAlertController *weakAlert = alert;
  [alert addAction:[UIAlertAction actionWithTitle:@"Cancel" style:UIAlertActionStyleCancel handler:nil]];
  [alert addAction:[UIAlertAction actionWithTitle:@"Save"
                                            style:UIAlertActionStyleDefault
                                          handler:^(UIAlertAction *a) {
                                            NSString *key = [weakAlert.textFields.firstObject.text
                                                stringByTrimmingCharactersInSet:
                                                    [NSCharacterSet whitespaceAndNewlineCharacterSet]];
                                            if (key.length) {
                                              GRSend(@{@"type" : @"agent_setup", @"agent_id" : identifier,
                                                       @"op" : @"api_key", @"secret" : key});
                                            }
                                          }]];
  [self presentViewController:alert animated:YES completion:nil];
}

- (void)createConnector
{
  UIAlertController *alert = [UIAlertController
      alertControllerWithTitle:@"Connect an external agent"
                       message:@"Name it after the client that will use it, e.g. \"ChatGPT\" or \"Claude desktop\"."
                preferredStyle:UIAlertControllerStyleAlert];
  [alert addTextFieldWithConfigurationHandler:^(UITextField *field) {
    field.placeholder = @"Name";
    field.autocapitalizationType = UITextAutocapitalizationTypeWords;
  }];
  __weak UIAlertController *weakAlert = alert;
  [alert addAction:[UIAlertAction actionWithTitle:@"Cancel" style:UIAlertActionStyleCancel handler:nil]];
  [alert addAction:[UIAlertAction actionWithTitle:@"Create"
                                            style:UIAlertActionStyleDefault
                                          handler:^(UIAlertAction *a) {
                                            NSString *name = [weakAlert.textFields.firstObject.text
                                                stringByTrimmingCharactersInSet:
                                                    [NSCharacterSet whitespaceAndNewlineCharacterSet]];
                                            if (name.length) {
                                              GRSend(@{@"type" : @"connector_create", @"name" : name});
                                            }
                                          }]];
  [self presentViewController:alert animated:YES completion:nil];
}

- (void)connectorActions:(NSDictionary *)connector source:(UIView *)source
{
  NSString *url = GRStr(connector[@"url"]);
  NSString *participant = GRStr(connector[@"participant_id"]);
  UIAlertController *sheet = [UIAlertController alertControllerWithTitle:GRStr(connector[@"name"])
                                                                 message:nil
                                                          preferredStyle:UIAlertControllerStyleActionSheet];
  if (url.length) {
    [sheet addAction:[UIAlertAction actionWithTitle:@"Copy connector URL"
                                              style:UIAlertActionStyleDefault
                                            handler:^(UIAlertAction *a) {
                                              [UIPasteboard generalPasteboard].string = url;
                                            }]];
    [sheet addAction:[UIAlertAction actionWithTitle:@"Share…"
                                              style:UIAlertActionStyleDefault
                                            handler:^(UIAlertAction *a) {
                                              UIActivityViewController *share = [[UIActivityViewController alloc]
                                                  initWithActivityItems:@[ url ]
                                                  applicationActivities:nil];
                                              share.popoverPresentationController.sourceView = source;
                                              [self presentViewController:share animated:YES completion:nil];
                                            }]];
  }
  if (!GRBool(connector[@"shared"])) {
    [sheet addAction:[UIAlertAction actionWithTitle:@"Revoke access"
                                              style:UIAlertActionStyleDestructive
                                            handler:^(UIAlertAction *a) {
                                              GRSend(@{@"type" : @"connector_revoke",
                                                       @"participant_id" : participant});
                                            }]];
  }
  [sheet addAction:[UIAlertAction actionWithTitle:@"Cancel" style:UIAlertActionStyleCancel handler:nil]];
  sheet.popoverPresentationController.sourceView = source;
  sheet.popoverPresentationController.sourceRect = source.bounds;
  [self presentViewController:sheet animated:YES completion:nil];
}

@end

/* ------------------------------------------------------------------------ cells */

@interface GRItemCell : UITableViewCell
@property(nonatomic, strong) UIView *card;
@property(nonatomic, strong) UIStackView *stack;
@property(nonatomic, strong) NSLayoutConstraint *leading;
@property(nonatomic, strong) NSLayoutConstraint *trailing;
@end

@implementation GRItemCell
- (instancetype)initWithStyle:(UITableViewCellStyle)style reuseIdentifier:(NSString *)identifier
{
  if ((self = [super initWithStyle:style reuseIdentifier:identifier])) {
    self.backgroundColor = [UIColor clearColor];
    self.selectionStyle = UITableViewCellSelectionStyleNone;
    self.card = [[UIView alloc] init];
    self.card.layer.cornerRadius = 16;
    self.card.layer.cornerCurve = kCACornerCurveContinuous;
    self.card.translatesAutoresizingMaskIntoConstraints = NO;
    [self.contentView addSubview:self.card];
    self.stack = GRStack(@[], UILayoutConstraintAxisVertical, 8);
    self.stack.translatesAutoresizingMaskIntoConstraints = NO;
    [self.card addSubview:self.stack];
    self.leading = [self.card.leadingAnchor constraintEqualToAnchor:self.contentView.leadingAnchor
                                                           constant:14];
    self.trailing = [self.card.trailingAnchor constraintEqualToAnchor:self.contentView.trailingAnchor
                                                             constant:-14];
    [NSLayoutConstraint activateConstraints:@[
      self.leading,
      self.trailing,
      [self.card.topAnchor constraintEqualToAnchor:self.contentView.topAnchor constant:5],
      [self.card.bottomAnchor constraintEqualToAnchor:self.contentView.bottomAnchor constant:-5],
      [self.stack.leadingAnchor constraintEqualToAnchor:self.card.leadingAnchor constant:14],
      [self.stack.trailingAnchor constraintEqualToAnchor:self.card.trailingAnchor constant:-14],
      [self.stack.topAnchor constraintEqualToAnchor:self.card.topAnchor constant:12],
      [self.stack.bottomAnchor constraintEqualToAnchor:self.card.bottomAnchor constant:-12],
    ]];
  }
  return self;
}
- (void)reset
{
  for (UIView *view in self.stack.arrangedSubviews) {
    [self.stack removeArrangedSubview:view];
    [view removeFromSuperview];
  }
  self.card.backgroundColor = GRCard();
  self.card.layer.borderWidth = 0;
  self.card.alpha = 1.0;
  self.leading.constant = 14;
  self.trailing.constant = -14;
}
@end

/* ------------------------------------------------------------------------ controller */

typedef NS_ENUM(NSInteger, GRDock) { GRDockRight = 0, GRDockLeft = 1 };

@interface GRController ()
@property(nonatomic, strong) UIWindow *window;
@property(nonatomic, weak) UIWindow *hostWindow;
@property(nonatomic, strong) UIViewController *agentsSheet;
@property(nonatomic, strong) NSDictionary *snapshot;
@property(nonatomic, strong) NSArray<NSDictionary *> *items;
@property(nonatomic, strong) NSMutableSet<NSString *> *expanded;
@property(nonatomic, strong) NSMutableSet<NSString *> *requestedFetches;
@property(nonatomic) NSInteger seenOpenRequest;
@property(nonatomic) BOOL panelOpen;
@property(nonatomic) BOOL sentReady;
@property(nonatomic) GRDock dock;
@property(nonatomic) CGFloat panelWidth;

/* Collapsed presence: always-available pill that shows live activity. */
@property(nonatomic, strong) UIVisualEffectView *pill;
@property(nonatomic, strong) UIImageView *pillIcon;
@property(nonatomic, strong) UILabel *pillLabel;
@property(nonatomic, strong) UIView *pillDot;
@property(nonatomic, strong) UIButton *pillStop;
@property(nonatomic, strong) NSLayoutConstraint *pillX;
@property(nonatomic, strong) NSLayoutConstraint *pillY;

/* Open workspace. */
@property(nonatomic, strong) UIVisualEffectView *panel;
@property(nonatomic, strong) NSLayoutConstraint *panelWidthConstraint;
@property(nonatomic, strong) NSLayoutConstraint *panelRight;
@property(nonatomic, strong) NSLayoutConstraint *panelLeft;
@property(nonatomic, strong) UIView *resizeHandle;
@property(nonatomic, strong) NSLayoutConstraint *handleRight;
@property(nonatomic, strong) NSLayoutConstraint *handleLeft;
@property(nonatomic, strong) UIView *connectionDot;
@property(nonatomic, strong) UILabel *connectionLabel;
@property(nonatomic, strong) UIButton *agentButton;
@property(nonatomic, strong) UIButton *modeButton;
@property(nonatomic, strong) UIButton *roleButton;
@property(nonatomic, strong) UIButton *dockButton;
@property(nonatomic, strong) UIView *activityBar;
@property(nonatomic, strong) UIImageView *activityIcon;
@property(nonatomic, strong) UILabel *activityLabel;
@property(nonatomic, strong) UILabel *activityDetail;
@property(nonatomic, strong) UILabel *activityElapsed;
@property(nonatomic, strong) UIButton *activityStop;
@property(nonatomic, strong) UIStackView *recoveryBox;
@property(nonatomic, strong) UIView *selectorsRow;
@property(nonatomic, strong) UIButton *agentsButton;
@property(nonatomic, strong) NSLayoutConstraint *panelBottom;
@property(nonatomic) CGFloat keyboardOverlap;
@property(nonatomic, strong) UIView *recoveryCard;
@property(nonatomic, strong) UITableView *table;
@property(nonatomic, strong) UILabel *emptyLabel;
@property(nonatomic, strong) UIScrollView *chipScroll;
@property(nonatomic, strong) UIStackView *chips;
@property(nonatomic, strong) GRTextView *composer;
@property(nonatomic, strong) UILabel *placeholder;
@property(nonatomic, strong) NSLayoutConstraint *composerHeight;
@property(nonatomic, strong) UIButton *attachButton;
@property(nonatomic, strong) UIButton *sendButton;
@property(nonatomic, strong) NSTimer *clock;
@property(nonatomic, copy) NSString *pendingSnapshot;
@property(nonatomic) BOOL retryScheduled;
@end

@implementation GRController

+ (instancetype)shared
{
  static GRController *controller;
  static dispatch_once_t once;
  dispatch_once(&once, ^{
    controller = [[GRController alloc] init];
  });
  return controller;
}

- (instancetype)init
{
  if ((self = [super init])) {
    self.items = @[];
    self.expanded = [NSMutableSet set];
    self.requestedFetches = [NSMutableSet set];
    NSUserDefaults *defaults = [NSUserDefaults standardUserDefaults];
    self.dock = (GRDock)[defaults integerForKey:@"GHOSTroom.dock"];
    CGFloat width = [defaults doubleForKey:@"GHOSTroom.width"];
    self.panelWidth = width >= 320 ? width : 440;
  }
  return self;
}

/* ---- installation into Blender's window ---- */

- (UIWindow *)blenderWindow
{
  UIWindow *fallback = nil;
  for (UIScene *scene in [UIApplication sharedApplication].connectedScenes) {
    if (![scene isKindOfClass:[UIWindowScene class]]) {
      continue;
    }
    for (UIWindow *candidate in ((UIWindowScene *)scene).windows) {
      if ([candidate isKindOfClass:[GROverlayWindow class]]) {
        continue;
      }
      if (candidate.isKeyWindow && candidate.rootViewController) {
        return candidate;
      }
      if (!fallback && candidate.rootViewController && !candidate.hidden) {
        fallback = candidate;
      }
    }
  }
  return fallback;
}

- (BOOL)install
{
  UIWindow *host = [self blenderWindow];
  if (!host || !host.windowScene) {
    return NO;
  }
  if (self.hostWindow == host && self.window.windowScene == host.windowScene && self.pill.superview) {
    return YES;
  }
  [self.pill removeFromSuperview];
  [self.panel removeFromSuperview];
  [self.resizeHandle removeFromSuperview];
  self.hostWindow = host;
  GROverlayWindow *overlay = [[GROverlayWindow alloc] initWithWindowScene:host.windowScene];
  overlay.rootViewController = [[GROverlayRoot alloc] init];
  overlay.windowLevel = host.windowLevel + 1;
  overlay.backgroundColor = [UIColor clearColor];
  overlay.hidden = NO;
  self.window = overlay;
  [self buildPill];
  [self buildPanel];
  [self installTouchObserver];
  if (!self.clock) {
    self.clock = [NSTimer scheduledTimerWithTimeInterval:1.0
                                                  target:self
                                                selector:@selector(tickClock)
                                                userInfo:nil
                                                 repeats:YES];
  }
  if (!self.sentReady) {
    self.sentReady = YES;
    GRSend(@{@"type" : @"ready"});
  }
  return YES;
}

- (void)keepOnTop
{
  if (!self.window) {
    return;
  }
  if (self.panel.superview == self.window) {
    [self.window bringSubviewToFront:self.panel];
    [self.window bringSubviewToFront:self.resizeHandle];
  }
  [self.window bringSubviewToFront:self.pill];
}

/* Touches on Blender end text entry so hardware keys return to Blender. The
 * observer fails immediately and never delays or cancels Blender's touches. */
- (void)installTouchObserver
{
  UITapGestureRecognizer *observer = [[UITapGestureRecognizer alloc] initWithTarget:self
                                                                             action:@selector(noop)];
  observer.cancelsTouchesInView = NO;
  observer.delaysTouchesBegan = NO;
  observer.delaysTouchesEnded = NO;
  observer.delegate = self;
  [self.hostWindow addGestureRecognizer:observer];
}

- (void)noop
{
}

- (BOOL)gestureRecognizer:(UIGestureRecognizer *)gestureRecognizer
       shouldReceiveTouch:(UITouch *)touch
{
  /* Only Blender's window carries this observer: any touch here is outside GHOSTroom. */
  if (self.composer.isFirstResponder) {
    [self endComposing];
  }
  return NO;
}

- (BOOL)gestureRecognizer:(UIGestureRecognizer *)gestureRecognizer
    shouldRecognizeSimultaneouslyWithGestureRecognizer:(UIGestureRecognizer *)other
{
  return YES;
}

/* ---- pill ---- */

- (void)buildPill
{
  UIVisualEffectView *pill = [[UIVisualEffectView alloc]
      initWithEffect:[UIBlurEffect effectWithStyle:UIBlurEffectStyleSystemUltraThinMaterialDark]];
  pill.translatesAutoresizingMaskIntoConstraints = NO;
  pill.layer.cornerRadius = 26;
  pill.layer.cornerCurve = kCACornerCurveContinuous;
  pill.clipsToBounds = YES;
  pill.layer.borderWidth = 1;
  pill.layer.borderColor = [GRMint() colorWithAlphaComponent:0.45].CGColor;
  pill.accessibilityLabel = @"Open GHOSTroom";
  pill.isAccessibilityElement = NO;

  self.pillIcon = [[UIImageView alloc] initWithImage:GRSymbol(@"sparkles", 18, UIFontWeightBold)];
  self.pillIcon.tintColor = GRMint();
  self.pillDot = [[UIView alloc] init];
  self.pillDot.backgroundColor = GRMint();
  self.pillDot.layer.cornerRadius = 4;
  self.pillDot.hidden = YES;
  self.pillLabel = GRLabel(14, UIFontWeightSemibold, GRInk(), 1);
  self.pillLabel.text = @"GHOSTroom";
  self.pillLabel.font = GRRounded(15, UIFontWeightBold);
  self.pillStop = [UIButton buttonWithType:UIButtonTypeSystem];
  [self.pillStop setImage:GRSymbol(@"stop.fill", 13, UIFontWeightBold) forState:UIControlStateNormal];
  self.pillStop.tintColor = GRCoral();
  self.pillStop.accessibilityLabel = @"Stop the agent";
  self.pillStop.hidden = YES;
  self.pillStop.pointerInteractionEnabled = YES;
  [self.pillStop addTarget:self action:@selector(stopActive) forControlEvents:UIControlEventPrimaryActionTriggered];

  UIStackView *row = GRStack(@[ self.pillIcon, self.pillDot, self.pillLabel, self.pillStop ],
                             UILayoutConstraintAxisHorizontal,
                             9);
  row.translatesAutoresizingMaskIntoConstraints = NO;
  [pill.contentView addSubview:row];
  [self.window addSubview:pill];
  self.pill = pill;

  UILayoutGuide *safe = self.window.safeAreaLayoutGuide;
  NSUserDefaults *defaults = [NSUserDefaults standardUserDefaults];
  CGFloat x = [defaults objectForKey:@"GHOSTroom.pillX"] ? [defaults doubleForKey:@"GHOSTroom.pillX"] : -24;
  CGFloat y = [defaults objectForKey:@"GHOSTroom.pillY"] ? [defaults doubleForKey:@"GHOSTroom.pillY"] : -96;
  self.pillX = [pill.trailingAnchor constraintEqualToAnchor:safe.trailingAnchor constant:x];
  self.pillY = [pill.bottomAnchor constraintEqualToAnchor:safe.bottomAnchor constant:y];
  [NSLayoutConstraint activateConstraints:@[
    self.pillX,
    self.pillY,
    [pill.heightAnchor constraintEqualToConstant:52],
    [pill.widthAnchor constraintLessThanOrEqualToConstant:420],
    [row.leadingAnchor constraintEqualToAnchor:pill.contentView.leadingAnchor constant:18],
    [row.trailingAnchor constraintEqualToAnchor:pill.contentView.trailingAnchor constant:-18],
    [row.centerYAnchor constraintEqualToAnchor:pill.contentView.centerYAnchor],
    [self.pillDot.widthAnchor constraintEqualToConstant:8],
    [self.pillDot.heightAnchor constraintEqualToConstant:8],
    [self.pillStop.widthAnchor constraintEqualToConstant:30],
    [self.pillStop.heightAnchor constraintEqualToConstant:30],
  ]];
  [pill addGestureRecognizer:[[UITapGestureRecognizer alloc] initWithTarget:self
                                                                     action:@selector(togglePanel)]];
  [pill addGestureRecognizer:[[UIPanGestureRecognizer alloc] initWithTarget:self
                                                                     action:@selector(dragPill:)]];
  [pill addInteraction:[[UIPointerInteraction alloc] initWithDelegate:nil]];
}

- (void)dragPill:(UIPanGestureRecognizer *)gesture
{
  CGPoint delta = [gesture translationInView:self.window];
  CGRect bounds = self.window.safeAreaLayoutGuide.layoutFrame;
  CGFloat minX = -(bounds.size.width - self.pill.bounds.size.width - 8);
  CGFloat minY = -(bounds.size.height - self.pill.bounds.size.height - 8);
  self.pillX.constant = MIN(-8, MAX(minX, self.pillX.constant + delta.x));
  self.pillY.constant = MIN(-8, MAX(minY, self.pillY.constant + delta.y));
  [gesture setTranslation:CGPointZero inView:self.window];
  if (gesture.state == UIGestureRecognizerStateEnded) {
    NSUserDefaults *defaults = [NSUserDefaults standardUserDefaults];
    [defaults setDouble:self.pillX.constant forKey:@"GHOSTroom.pillX"];
    [defaults setDouble:self.pillY.constant forKey:@"GHOSTroom.pillY"];
  }
}

/* ---- panel ---- */

- (void)buildPanel
{
  UIVisualEffectView *panel = [[UIVisualEffectView alloc]
      initWithEffect:[UIBlurEffect effectWithStyle:UIBlurEffectStyleSystemThinMaterialDark]];
  panel.translatesAutoresizingMaskIntoConstraints = NO;
  panel.layer.cornerRadius = 24;
  panel.layer.cornerCurve = kCACornerCurveContinuous;
  panel.clipsToBounds = YES;
  panel.layer.borderWidth = 1;
  panel.layer.borderColor = [UIColor colorWithWhite:1 alpha:0.10].CGColor;
  panel.contentView.backgroundColor = GRSurface();
  panel.hidden = YES;
  panel.alpha = 0;
  self.panel = panel;
  UIView *content = panel.contentView;

  /* Header: identity, connection, agent/mode/role selection, dock, minimise. */
  UILabel *title = GRLabel(22, UIFontWeightBold, GRInk(), 1);
  title.font = GRRounded(22, UIFontWeightHeavy);
  NSMutableAttributedString *wordmark =
      [[NSMutableAttributedString alloc] initWithString:@"GHOST"
                                             attributes:@{NSForegroundColorAttributeName : GRMint()}];
  [wordmark appendAttributedString:[[NSAttributedString alloc]
                                       initWithString:@"room"
                                           attributes:@{NSForegroundColorAttributeName : GRInk()}]];
  title.attributedText = wordmark;
  self.connectionDot = [[UIView alloc] init];
  self.connectionDot.layer.cornerRadius = 4;
  self.connectionDot.backgroundColor = GRMuted();
  self.connectionLabel = GRLabel(12, UIFontWeightMedium, GRMuted(), 1);
  self.connectionLabel.text = @"Connecting…";
  UIStackView *connection = GRStack(@[ self.connectionDot, self.connectionLabel ],
                                    UILayoutConstraintAxisHorizontal,
                                    6);
  UIStackView *titles = GRStack(@[ title, connection ], UILayoutConstraintAxisVertical, 2);
  titles.alignment = UIStackViewAlignmentLeading;

  self.dockButton = GRPlainButton(nil, @"rectangle.lefthalf.inset.filled", GRMuted());
  self.dockButton.accessibilityLabel = @"Move GHOSTroom to the other side";
  [self.dockButton addTarget:self action:@selector(toggleDock) forControlEvents:UIControlEventPrimaryActionTriggered];
  UIButton *minimise = GRPlainButton(nil, @"chevron.down.circle.fill", GRMuted());
  minimise.accessibilityLabel = @"Minimise GHOSTroom and return to Blender";
  [minimise addTarget:self action:@selector(togglePanel) forControlEvents:UIControlEventPrimaryActionTriggered];
  UIButton *agents = GRTintedButton(@"Agents", @"person.2.badge.gearshape", GRViolet());
  agents.accessibilityLabel = @"Agents and connections: sign in, API keys, external agents";
  [agents addTarget:self action:@selector(showAgents) forControlEvents:UIControlEventPrimaryActionTriggered];
  self.agentsButton = agents;
  UIStackView *header = GRStack(@[ titles, GRSpacer(), agents, self.dockButton, minimise ],
                                UILayoutConstraintAxisHorizontal,
                                4);

  self.agentButton = GRTintedButton(@"Agent", @"person.crop.circle", GRMint());
  self.agentButton.showsMenuAsPrimaryAction = YES;
  self.agentButton.changesSelectionAsPrimaryAction = NO;
  self.modeButton = GRTintedButton(@"Do it for me", @"wand.and.stars", GRViolet());
  self.modeButton.showsMenuAsPrimaryAction = YES;
  self.roleButton = GRTintedButton(@"Primary", @"person.2", GRMuted());
  self.roleButton.showsMenuAsPrimaryAction = YES;
  UIStackView *selectors = GRStack(@[ self.agentButton, self.modeButton, self.roleButton, GRSpacer() ],
                                   UILayoutConstraintAxisHorizontal,
                                   8);

  /* Live activity: what the selected agent is doing right now. */
  self.activityBar = [[UIView alloc] init];
  self.activityBar.backgroundColor = GRCard();
  self.activityBar.layer.cornerRadius = 14;
  self.activityBar.layer.cornerCurve = kCACornerCurveContinuous;
  self.activityIcon = [[UIImageView alloc] initWithImage:GRSymbol(@"sparkles", 17, UIFontWeightBold)];
  self.activityIcon.tintColor = GRMint();
  self.activityIcon.contentMode = UIViewContentModeCenter;
  self.activityLabel = GRLabel(15, UIFontWeightSemibold, GRInk(), 1);
  self.activityDetail = GRLabel(13, UIFontWeightRegular, GRMuted(), 2);
  self.activityElapsed = GRLabel(12, UIFontWeightMedium, GRMuted(), 1);
  self.activityElapsed.font = [UIFont monospacedDigitSystemFontOfSize:12 weight:UIFontWeightMedium];
  self.activityStop = GRTintedButton(@"Stop", @"stop.fill", GRCoral());
  self.activityStop.accessibilityLabel = @"Stop the active task";
  [self.activityStop addTarget:self action:@selector(stopActive) forControlEvents:UIControlEventPrimaryActionTriggered];
  UIStackView *activityText = GRStack(@[ self.activityLabel, self.activityDetail ],
                                      UILayoutConstraintAxisVertical,
                                      1);
  UIStackView *activityRow = GRStack(@[ self.activityIcon, activityText, GRSpacer(), self.activityElapsed,
                                        self.activityStop ],
                                     UILayoutConstraintAxisHorizontal,
                                     10);
  activityRow.translatesAutoresizingMaskIntoConstraints = NO;
  [self.activityBar addSubview:activityRow];
  [NSLayoutConstraint activateConstraints:@[
    [activityRow.leadingAnchor constraintEqualToAnchor:self.activityBar.leadingAnchor constant:12],
    [activityRow.trailingAnchor constraintEqualToAnchor:self.activityBar.trailingAnchor constant:-10],
    [activityRow.topAnchor constraintEqualToAnchor:self.activityBar.topAnchor constant:10],
    [activityRow.bottomAnchor constraintEqualToAnchor:self.activityBar.bottomAnchor constant:-10],
    [self.activityIcon.widthAnchor constraintEqualToConstant:24],
  ]];

  self.recoveryBox = GRStack(@[], UILayoutConstraintAxisVertical, 0);

  /* Conversation and activity timeline. */
  self.table = [[UITableView alloc] initWithFrame:CGRectZero style:UITableViewStylePlain];
  self.table.backgroundColor = [UIColor clearColor];
  self.table.separatorStyle = UITableViewCellSeparatorStyleNone;
  self.table.dataSource = self;
  self.table.delegate = self;
  self.table.rowHeight = UITableViewAutomaticDimension;
  self.table.estimatedRowHeight = 90;
  self.table.keyboardDismissMode = UIScrollViewKeyboardDismissModeInteractive;
  self.table.allowsSelection = NO;
  [self.table registerClass:[GRItemCell class] forCellReuseIdentifier:@"item"];
  self.emptyLabel = GRLabel(15, UIFontWeightRegular, GRMuted(), 0);
  self.emptyLabel.textAlignment = NSTextAlignmentCenter;
  self.emptyLabel.text = @"Talk to an agent about the scene you are working on.\n"
                         @"It can inspect and work on the live Blender project while you watch, "
                         @"redirect it or stop it at any time.";
  self.table.backgroundView = self.emptyLabel;

  /* Composer: attachments, large multiline text, send. */
  UIView *composerCard = [[UIView alloc] init];
  composerCard.backgroundColor = GRCard();
  composerCard.layer.cornerRadius = 18;
  composerCard.layer.cornerCurve = kCACornerCurveContinuous;
  composerCard.layer.borderWidth = 1;
  composerCard.layer.borderColor = [UIColor colorWithWhite:1 alpha:0.08].CGColor;

  self.chips = GRStack(@[], UILayoutConstraintAxisHorizontal, 8);
  self.chipScroll = [[UIScrollView alloc] init];
  self.chipScroll.showsHorizontalScrollIndicator = NO;
  self.chipScroll.hidden = YES;
  self.chips.translatesAutoresizingMaskIntoConstraints = NO;
  [self.chipScroll addSubview:self.chips];

  self.composer = [[GRTextView alloc] init];
  self.composer.font = GRFont(17, UIFontWeightRegular);
  self.composer.textColor = GRInk();
  self.composer.backgroundColor = [UIColor clearColor];
  self.composer.tintColor = GRMint();
  self.composer.delegate = self;
  self.composer.scrollEnabled = NO;
  self.composer.keyboardAppearance = UIKeyboardAppearanceDark;
  self.composer.autocorrectionType = UITextAutocorrectionTypeDefault;
  self.composer.textContainerInset = UIEdgeInsetsMake(10, 4, 10, 4);
  self.composer.accessibilityLabel = @"Message to the agent";
  __weak GRController *weakSelf = self;
  self.composer.onSubmit = ^{
    [weakSelf sendMessage];
  };
  self.placeholder = GRLabel(17, UIFontWeightRegular, [GRMuted() colorWithAlphaComponent:0.7], 1);
  self.placeholder.text = @"Ask, instruct or redirect…";
  self.placeholder.translatesAutoresizingMaskIntoConstraints = NO;
  [self.composer addSubview:self.placeholder];

  self.attachButton = GRPlainButton(nil, @"plus.circle.fill", GRMint());
  self.attachButton.showsMenuAsPrimaryAction = YES;
  self.attachButton.accessibilityLabel = @"Attach context";
  self.attachButton.menu = [self attachMenu];
  self.sendButton = GRPlainButton(nil, @"arrow.up.circle.fill", GRMint());
  self.sendButton.accessibilityLabel = @"Send";
  [self.sendButton addTarget:self action:@selector(sendMessage) forControlEvents:UIControlEventPrimaryActionTriggered];
  UILabel *hint = GRLabel(11, UIFontWeightMedium, [GRMuted() colorWithAlphaComponent:0.8], 1);
  hint.text = @"⌘↩ send · Return for a new line";
  UIStackView *tools = GRStack(@[ self.attachButton, hint, GRSpacer(), self.sendButton ],
                               UILayoutConstraintAxisHorizontal,
                               6);
  UIStackView *composerStack = GRStack(@[ self.chipScroll, self.composer, tools ],
                                       UILayoutConstraintAxisVertical,
                                       2);
  composerStack.translatesAutoresizingMaskIntoConstraints = NO;
  [composerCard addSubview:composerStack];

  self.selectorsRow = selectors;
  UIStackView *top = GRStack(@[ header, selectors, self.activityBar, self.recoveryBox ],
                             UILayoutConstraintAxisVertical,
                             10);
  top.translatesAutoresizingMaskIntoConstraints = NO;
  self.table.translatesAutoresizingMaskIntoConstraints = NO;
  composerCard.translatesAutoresizingMaskIntoConstraints = NO;
  [content addSubview:top];
  [content addSubview:self.table];
  [content addSubview:composerCard];

  self.composerHeight = [self.composer.heightAnchor constraintEqualToConstant:46];
  [NSLayoutConstraint activateConstraints:@[
    [top.leadingAnchor constraintEqualToAnchor:content.leadingAnchor constant:16],
    [top.trailingAnchor constraintEqualToAnchor:content.trailingAnchor constant:-12],
    [top.topAnchor constraintEqualToAnchor:content.topAnchor constant:14],
    [self.table.topAnchor constraintEqualToAnchor:top.bottomAnchor constant:6],
    [self.table.leadingAnchor constraintEqualToAnchor:content.leadingAnchor],
    [self.table.trailingAnchor constraintEqualToAnchor:content.trailingAnchor],
    [composerCard.topAnchor constraintEqualToAnchor:self.table.bottomAnchor constant:6],
    [composerCard.leadingAnchor constraintEqualToAnchor:content.leadingAnchor constant:12],
    [composerCard.trailingAnchor constraintEqualToAnchor:content.trailingAnchor constant:-12],
    [composerCard.bottomAnchor constraintEqualToAnchor:content.bottomAnchor constant:-12],
    [composerStack.leadingAnchor constraintEqualToAnchor:composerCard.leadingAnchor constant:8],
    [composerStack.trailingAnchor constraintEqualToAnchor:composerCard.trailingAnchor constant:-6],
    [composerStack.topAnchor constraintEqualToAnchor:composerCard.topAnchor constant:6],
    [composerStack.bottomAnchor constraintEqualToAnchor:composerCard.bottomAnchor constant:-4],
    self.composerHeight,
    [self.placeholder.leadingAnchor constraintEqualToAnchor:self.composer.leadingAnchor constant:9],
    [self.placeholder.topAnchor constraintEqualToAnchor:self.composer.topAnchor constant:10],
    [self.chipScroll.heightAnchor constraintEqualToConstant:64],
    [self.chips.leadingAnchor constraintEqualToAnchor:self.chipScroll.contentLayoutGuide.leadingAnchor],
    [self.chips.trailingAnchor constraintEqualToAnchor:self.chipScroll.contentLayoutGuide.trailingAnchor],
    [self.chips.topAnchor constraintEqualToAnchor:self.chipScroll.contentLayoutGuide.topAnchor constant:6],
    [self.chips.bottomAnchor constraintEqualToAnchor:self.chipScroll.contentLayoutGuide.bottomAnchor],
    [self.chips.heightAnchor constraintEqualToAnchor:self.chipScroll.frameLayoutGuide.heightAnchor constant:-6],
    [self.connectionDot.widthAnchor constraintEqualToConstant:8],
    [self.connectionDot.heightAnchor constraintEqualToConstant:8],
  ]];
  NSLayoutConstraint *tableMinimum = [self.table.heightAnchor constraintGreaterThanOrEqualToConstant:120];
  tableMinimum.priority = UILayoutPriorityDefaultLow;
  tableMinimum.active = YES;

  [self.window addSubview:panel];
  UILayoutGuide *safe = self.window.safeAreaLayoutGuide;
  self.panelWidthConstraint = [panel.widthAnchor constraintEqualToConstant:self.panelWidth];
  self.panelWidthConstraint.priority = UILayoutPriorityDefaultHigh;
  self.panelRight = [panel.trailingAnchor constraintEqualToAnchor:safe.trailingAnchor constant:-12];
  self.panelLeft = [panel.leadingAnchor constraintEqualToAnchor:safe.leadingAnchor constant:12];
  /* The bottom edge follows the software keyboard explicitly (keyboardWillChange:), as a
   * required constraint, so the composer can never end up underneath the keyboard. */
  self.panelBottom = [panel.bottomAnchor constraintEqualToAnchor:safe.bottomAnchor constant:-12];
  NSLayoutConstraint *panelTop = [panel.topAnchor constraintEqualToAnchor:safe.topAnchor constant:12];
  panelTop.priority = UILayoutPriorityRequired - 1;
  [NSLayoutConstraint activateConstraints:@[
    self.panelWidthConstraint,
    [panel.widthAnchor constraintLessThanOrEqualToAnchor:safe.widthAnchor multiplier:0.72],
    panelTop,
    [panel.topAnchor constraintGreaterThanOrEqualToAnchor:self.window.topAnchor],
    self.panelBottom,
  ]];
  NSNotificationCenter *center = [NSNotificationCenter defaultCenter];
  [center addObserver:self
             selector:@selector(keyboardWillChange:)
                 name:UIKeyboardWillChangeFrameNotification
               object:nil];
  [center addObserver:self
             selector:@selector(keyboardWillChange:)
                 name:UIKeyboardWillHideNotification
               object:nil];

  /* Resize handle on the inner edge: Blender keeps as much screen as the user wants. */
  self.resizeHandle = [[UIView alloc] init];
  self.resizeHandle.translatesAutoresizingMaskIntoConstraints = NO;
  self.resizeHandle.hidden = YES;
  UIView *grip = [[UIView alloc] init];
  grip.translatesAutoresizingMaskIntoConstraints = NO;
  grip.backgroundColor = [GRInk() colorWithAlphaComponent:0.45];
  grip.layer.cornerRadius = 2.5;
  [self.resizeHandle addSubview:grip];
  [self.window addSubview:self.resizeHandle];
  self.handleRight = [self.resizeHandle.centerXAnchor constraintEqualToAnchor:panel.leadingAnchor];
  self.handleLeft = [self.resizeHandle.centerXAnchor constraintEqualToAnchor:panel.trailingAnchor];
  [NSLayoutConstraint activateConstraints:@[
    [self.resizeHandle.widthAnchor constraintEqualToConstant:28],
    [self.resizeHandle.heightAnchor constraintEqualToConstant:120],
    [self.resizeHandle.centerYAnchor constraintEqualToAnchor:panel.centerYAnchor],
    [grip.widthAnchor constraintEqualToConstant:5],
    [grip.heightAnchor constraintEqualToConstant:56],
    [grip.centerXAnchor constraintEqualToAnchor:self.resizeHandle.centerXAnchor],
    [grip.centerYAnchor constraintEqualToAnchor:self.resizeHandle.centerYAnchor],
  ]];
  [self.resizeHandle addGestureRecognizer:[[UIPanGestureRecognizer alloc]
                                              initWithTarget:self
                                                      action:@selector(resizePanel:)]];
  [self.resizeHandle addInteraction:[[UIPointerInteraction alloc] initWithDelegate:nil]];
  [self applyDock];
}

- (void)keyboardWillChange:(NSNotification *)note
{
  if (!self.window) {
    return;
  }
  CGRect screenFrame = [note.userInfo[UIKeyboardFrameEndUserInfoKey] CGRectValue];
  CGRect frame = [self.window convertRect:screenFrame fromCoordinateSpace:self.window.screen.coordinateSpace];
  CGRect bounds = self.window.bounds;
  CGFloat overlap = 0;
  BOOL hiding = [note.name isEqualToString:UIKeyboardWillHideNotification];
  /* A docked keyboard spans the width and reaches the bottom; a floating one does not
   * push the panel. */
  if (!hiding && CGRectIntersectsRect(frame, bounds) && frame.size.width >= bounds.size.width * 0.6 &&
      CGRectGetMaxY(frame) >= CGRectGetMaxY(bounds) - 1)
  {
    overlap = MAX(0, CGRectGetMaxY(bounds) - CGRectGetMinY(frame));
  }
  self.keyboardOverlap = overlap;
  CGFloat safeBottom = self.window.safeAreaInsets.bottom;
  self.panelBottom.constant = -(MAX(overlap - safeBottom, 0) + 12);
  BOOL compact = overlap > 0;
  NSTimeInterval duration = [note.userInfo[UIKeyboardAnimationDurationUserInfoKey] doubleValue];
  [UIView animateWithDuration:duration > 0 ? duration : 0.25
                   animations:^{
                     self.selectorsRow.hidden = compact;
                     self.recoveryBox.hidden = compact;
                     [self textViewDidChange:self.composer];
                     [self.window layoutIfNeeded];
                   }
                   completion:^(BOOL finished) {
                     if (compact && self.items.count) {
                       [self.table scrollToRowAtIndexPath:[NSIndexPath indexPathForRow:(NSInteger)self.items.count - 1
                                                                             inSection:0]
                                         atScrollPosition:UITableViewScrollPositionBottom
                                                 animated:YES];
                     }
                   }];
}

- (void)applyDock
{
  BOOL right = self.dock == GRDockRight;
  self.panelRight.active = right;
  self.panelLeft.active = !right;
  self.handleRight.active = right;
  self.handleLeft.active = !right;
  UIButtonConfiguration *config = self.dockButton.configuration;
  config.image = GRSymbol(right ? @"rectangle.lefthalf.inset.filled" : @"rectangle.righthalf.inset.filled",
                          15,
                          UIFontWeightSemibold);
  self.dockButton.configuration = config;
  [[NSUserDefaults standardUserDefaults] setInteger:self.dock forKey:@"GHOSTroom.dock"];
}

- (void)toggleDock
{
  self.dock = self.dock == GRDockRight ? GRDockLeft : GRDockRight;
  [UIView animateWithDuration:0.25
                   animations:^{
                     [self applyDock];
                     [self.window layoutIfNeeded];
                   }];
}

- (void)resizePanel:(UIPanGestureRecognizer *)gesture
{
  CGFloat dx = [gesture translationInView:self.window].x;
  CGFloat width = self.panelWidthConstraint.constant + (self.dock == GRDockRight ? -dx : dx);
  CGFloat maximum = self.window.bounds.size.width * 0.72;
  self.panelWidthConstraint.constant = MAX(320, MIN(maximum, width));
  [gesture setTranslation:CGPointZero inView:self.window];
  if (gesture.state == UIGestureRecognizerStateEnded) {
    self.panelWidth = self.panelWidthConstraint.constant;
    [[NSUserDefaults standardUserDefaults] setDouble:self.panelWidth forKey:@"GHOSTroom.width"];
  }
}

- (void)togglePanel
{
  [self setPanelOpen:!self.panelOpen animated:YES];
}

- (void)setPanelOpen:(BOOL)open animated:(BOOL)animated
{
  if (!self.panel) {
    return;
  }
  self.panelOpen = open;
  GRSend(@{@"type" : @"visible", @"open" : @(open)});
  if (!open) {
    [self endComposing];
  }
  if (open) {
    self.panel.hidden = NO;
    self.resizeHandle.hidden = NO;
    [self keepOnTop];
    [self reloadTimeline:YES];
  }
  CGFloat offset = self.dock == GRDockRight ? 40 : -40;
  if (open) {
    self.panel.transform = CGAffineTransformMakeTranslation(offset, 0);
  }
  void (^changes)(void) = ^{
    self.panel.alpha = open ? 1 : 0;
    self.panel.transform = open ? CGAffineTransformIdentity : CGAffineTransformMakeTranslation(offset, 0);
    self.pill.alpha = open ? 0 : 1;
  };
  void (^done)(BOOL) = ^(BOOL finished) {
    if (!self.panelOpen) {
      self.panel.hidden = YES;
      self.resizeHandle.hidden = YES;
      self.panel.transform = CGAffineTransformIdentity;
    }
    self.pill.hidden = self.panelOpen;
  };
  self.pill.hidden = NO;
  if (animated) {
    [UIView animateWithDuration:0.28
                          delay:0
         usingSpringWithDamping:0.9
          initialSpringVelocity:0.4
                        options:UIViewAnimationOptionAllowUserInteraction
                     animations:changes
                     completion:done];
  }
  else {
    changes();
    done(YES);
  }
}

/* Typing needs GHOSTroom's window to be key; hardware shortcuts go back to Blender after. */
- (BOOL)textViewShouldBeginEditing:(UITextView *)textView
{
  if (!self.window.isKeyWindow) {
    [self.window makeKeyWindow];
  }
  return YES;
}

- (void)returnKeyboardToBlender
{
  if (self.hostWindow && !self.hostWindow.isKeyWindow) {
    [self.hostWindow makeKeyWindow];
  }
  [self.hostWindow.rootViewController becomeFirstResponder];
}

- (void)endComposing
{
  if (self.composer.isFirstResponder) {
    [self.composer resignFirstResponder];
  }
  [self returnKeyboardToBlender];
}

- (void)textViewDidEndEditing:(UITextView *)textView
{
  [self returnKeyboardToBlender];
}

/* ---- snapshot ---- */

- (void)applySnapshot:(NSString *)json
{
  if (![self install]) {
    /* Blender's window may not exist yet during startup: keep the latest snapshot and retry. */
    self.pendingSnapshot = json;
    if (!self.retryScheduled) {
      self.retryScheduled = YES;
      dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.5 * NSEC_PER_SEC)),
                     dispatch_get_main_queue(),
                     ^{
                       self.retryScheduled = NO;
                       NSString *pending = self.pendingSnapshot;
                       self.pendingSnapshot = nil;
                       if (pending) {
                         [self applySnapshot:pending];
                       }
                     });
    }
    return;
  }
  NSData *data = [json dataUsingEncoding:NSUTF8StringEncoding];
  NSDictionary *snapshot = GRDict([NSJSONSerialization JSONObjectWithData:data options:0 error:nil]);
  if (!snapshot) {
    return;
  }
  NSDictionary *previous = self.snapshot;
  self.snapshot = snapshot;
  NSInteger openRequest = (NSInteger)GRNum(snapshot[@"open_request"]);
  if (openRequest > self.seenOpenRequest) {
    self.seenOpenRequest = openRequest;
    if (!self.panelOpen && previous) {
      [self setPanelOpen:YES animated:YES];
    }
  }
  [self keepOnTop];
  [self renderHeader];
  [self renderActivity];
  [self renderPill];
  [self renderRecovery];
  [self renderAttachments];
  NSArray *items = GRArr(snapshot[@"items"]);
  if (![items isEqualToArray:self.items]) {
    NSString *lastBefore = GRStr(GRDict(self.items.lastObject)[@"id"]);
    self.items = items;
    NSString *lastNow = GRStr(GRDict(items.lastObject)[@"id"]);
    [self reloadTimeline:![lastBefore isEqualToString:lastNow]];
  }
}

- (void)reloadTimeline:(BOOL)follow
{
  if (!self.panelOpen) {
    return;
  }
  UITableView *table = self.table;
  BOOL nearBottom = table.contentOffset.y + table.bounds.size.height >=
                    table.contentSize.height - 80;
  CGPoint offset = table.contentOffset;
  self.emptyLabel.hidden = self.items.count > 0;
  [table reloadData];
  [table layoutIfNeeded];
  if ((follow || nearBottom) && self.items.count > 0) {
    [table scrollToRowAtIndexPath:[NSIndexPath indexPathForRow:(NSInteger)self.items.count - 1 inSection:0]
                 atScrollPosition:UITableViewScrollPositionBottom
                         animated:NO];
  }
  else {
    table.contentOffset = offset;
  }
}

- (NSDictionary *)selectedAgent
{
  NSString *selected = GRStr(GRDict(self.snapshot[@"selected"])[@"agent_id"]);
  for (NSDictionary *agent in GRArr(self.snapshot[@"agents"])) {
    if ([GRStr(agent[@"id"]) isEqualToString:selected]) {
      return agent;
    }
  }
  return nil;
}

- (void)setButton:(UIButton *)button title:(NSString *)title
{
  UIButtonConfiguration *config = button.configuration;
  config.title = title;
  button.configuration = config;
}

- (void)renderHeader
{
  NSDictionary *connection = GRDict(self.snapshot[@"connection"]);
  NSString *state = GRStr(connection[@"state"]);
  UIColor *color = [state isEqualToString:@"connected"] ? GRMint() :
                   ([state isEqualToString:@"offline"] ? GRCoral() : GRAmber());
  self.connectionDot.backgroundColor = color;
  NSString *label = GRStr(connection[@"label"]);
  NSDictionary *agent = [self selectedAgent];
  if (agent) {
    NSString *agentState = GRStr(agent[@"state"]);
    NSString *model = GRStr(agent[@"model"]);
    label = [NSString stringWithFormat:@"%@ · %@ %@%@",
                                       label,
                                       GRStr(agent[@"name"]),
                                       agentState,
                                       model.length ? [@" · " stringByAppendingString:model] : @""];
  }
  self.connectionLabel.text = label;

  /* Agent picker: each agent with availability, auth and quota before any turn fails. */
  NSMutableArray *actions = [NSMutableArray array];
  NSString *selectedId = GRStr(agent[@"id"]);
  for (NSDictionary *item in GRArr(self.snapshot[@"agents"])) {
    NSString *identifier = GRStr(item[@"id"]);
    NSString *itemState = GRStr(item[@"state"]);
    NSString *auth = GRStr(item[@"auth"]);
    NSString *quota = GRStr(item[@"quota"]);
    NSMutableArray *parts = [NSMutableArray arrayWithObject:itemState.capitalizedString];
    if (GRStr(item[@"model"]).length) {
      [parts addObject:GRStr(item[@"model"])];
    }
    if (![auth isEqualToString:@"signed_in"] && ![auth isEqualToString:@"unknown"]) {
      [parts addObject:[auth stringByReplacingOccurrencesOfString:@"_" withString:@" "]];
    }
    if ([quota isEqualToString:@"exhausted"] || [quota isEqualToString:@"low"]) {
      [parts addObject:[@"quota " stringByAppendingString:quota]];
    }
    if (GRStr(item[@"activity"]).length) {
      [parts addObject:GRStr(item[@"activity"])];
    }
    if (GRStr(item[@"reason"]).length) {
      [parts addObject:GRStr(item[@"reason"])];
    }
    UIColor *dot = [itemState isEqualToString:@"available"] ? GRMint() :
                   ([itemState isEqualToString:@"busy"] ? GRViolet() :
                    ([itemState isEqualToString:@"unavailable"] ? GRCoral() : GRAmber()));
    UIImage *image = [GRSymbol(@"circle.fill", 10, UIFontWeightBold)
        imageWithTintColor:dot
             renderingMode:UIImageRenderingModeAlwaysOriginal];
    UIAction *action = [UIAction actionWithTitle:GRStr(item[@"name"])
                                           image:image
                                      identifier:nil
                                         handler:^(UIAction *a) {
                                           GRSend(@{@"type" : @"select", @"agent_id" : identifier});
                                         }];
    action.subtitle = [parts componentsJoinedByString:@" · "];
    action.state = [identifier isEqualToString:selectedId] ? UIMenuElementStateOn : UIMenuElementStateOff;
    [actions addObject:action];
  }
  __weak GRController *weakSelf = self;
  UIAction *manage = [UIAction actionWithTitle:@"Agents & connections…"
                                         image:GRSymbol(@"person.2.badge.gearshape", 15, UIFontWeightMedium)
                                    identifier:nil
                                       handler:^(UIAction *a) {
                                         [weakSelf showAgents];
                                       }];
  if (actions.count == 0) {
    UIAction *none = [UIAction actionWithTitle:@"No agents configured on the relay"
                                         image:nil
                                    identifier:nil
                                       handler:^(UIAction *a){
                                       }];
    none.attributes = UIMenuElementAttributesDisabled;
    [actions addObject:none];
  }
  UIMenu *agentsMenu = [UIMenu menuWithTitle:@"" image:nil identifier:nil
                                     options:UIMenuOptionsDisplayInline children:actions];
  UIMenu *manageMenu = [UIMenu menuWithTitle:@"" image:nil identifier:nil
                                     options:UIMenuOptionsDisplayInline children:@[ manage ]];
  self.agentButton.menu = [UIMenu menuWithTitle:@"Talk to" children:@[ agentsMenu, manageMenu ]];
  [(GRAgentsSheet *)((UINavigationController *)self.agentsSheet).viewControllers.firstObject
      refresh:GRDict(self.snapshot[@"setup"])];
  NSString *agentState = GRStr(agent[@"state"]);
  [self setButton:self.agentButton title:agent ? GRStr(agent[@"name"]) : @"Agent"];
  UIButtonConfiguration *config = self.agentButton.configuration;
  UIColor *tint = [agentState isEqualToString:@"unavailable"] ? GRCoral() : GRMint();
  config.baseForegroundColor = tint;
  config.baseBackgroundColor = tint;
  self.agentButton.configuration = config;

  NSDictionary *selected = GRDict(self.snapshot[@"selected"]);
  self.modeButton.menu = [self choiceMenu:GRArr(self.snapshot[@"modes"])
                                    title:@"How to work"
                                      key:@"mode"
                                 selected:GRStr(selected[@"mode"])
                                   button:self.modeButton];
  self.roleButton.menu = [self choiceMenu:GRArr(self.snapshot[@"roles"])
                                    title:@"Agent role"
                                      key:@"role"
                                 selected:GRStr(selected[@"role"])
                                   button:self.roleButton];
}

- (UIMenu *)choiceMenu:(NSArray *)choices
                 title:(NSString *)title
                   key:(NSString *)key
              selected:(NSString *)selected
                button:(UIButton *)button
{
  NSMutableArray *actions = [NSMutableArray array];
  for (NSDictionary *choice in choices) {
    NSString *identifier = GRStr(choice[@"id"]);
    NSString *label = GRStr(choice[@"label"]);
    UIAction *action = [UIAction actionWithTitle:label
                                           image:nil
                                      identifier:nil
                                         handler:^(UIAction *a) {
                                           GRSend(@{@"type" : @"select", key : identifier});
                                         }];
    action.state = [identifier isEqualToString:selected] ? UIMenuElementStateOn : UIMenuElementStateOff;
    if ([identifier isEqualToString:selected]) {
      [self setButton:button title:label];
    }
    [actions addObject:action];
  }
  return [UIMenu menuWithTitle:title children:actions];
}

- (void)renderActivity
{
  NSDictionary *activity = GRDict(self.snapshot[@"activity"]);
  NSString *state = GRStr(activity[@"state"]);
  NSDictionary *connection = GRDict(self.snapshot[@"connection"]);
  NSString *connectionState = GRStr(connection[@"state"]);
  NSString *label = GRStr(activity[@"label"]);
  NSString *detail = GRStr(activity[@"detail"]);
  if ([state isEqualToString:@"idle"] && ![connectionState isEqualToString:@"connected"]) {
    state = [connectionState isEqualToString:@"offline"] ? @"failed" : @"waiting";
    label = GRStr(connection[@"label"]);
    detail = GRStr(connection[@"detail"]);
  }
  NSString *agent = GRStr(activity[@"agent"]);
  if (agent.length && ![state isEqualToString:@"idle"]) {
    label = [NSString stringWithFormat:@"%@ · %@", agent, label];
  }
  UIColor *color = GRStateColor(state);
  self.activityIcon.image = GRSymbol(GRStateSymbol(state), 17, UIFontWeightBold);
  self.activityIcon.tintColor = color;
  self.activityLabel.text = label.length ? label : @"Ready";
  self.activityDetail.text = detail;
  self.activityDetail.hidden = detail.length == 0;
  self.activityStop.hidden = !GRBool(activity[@"can_stop"]);
  self.activityBar.layer.borderWidth = [state isEqualToString:@"idle"] ? 0 : 1;
  self.activityBar.layer.borderColor = [color colorWithAlphaComponent:0.5].CGColor;
  [self tickClock];
}

- (void)tickClock
{
  NSDictionary *activity = GRDict(self.snapshot[@"activity"]);
  double since = GRNum(activity[@"since"]);
  self.activityElapsed.text = since > 0 ? GRElapsed(since) : @"";
  self.activityElapsed.hidden = since <= 0;
  NSString *state = GRStr(activity[@"state"]);
  BOOL live = !([state isEqualToString:@"idle"] || [state isEqualToString:@"failed"] ||
                [state isEqualToString:@"stopped"] || state.length == 0);
  if (live && !self.pillDot.hidden) {
    [UIView animateWithDuration:0.45
                     animations:^{
                       self.pillDot.alpha = self.pillDot.alpha > 0.6 ? 0.25 : 1.0;
                     }];
  }
}

- (void)renderPill
{
  NSDictionary *activity = GRDict(self.snapshot[@"activity"]);
  NSString *state = GRStr(activity[@"state"]);
  BOOL idle = [state isEqualToString:@"idle"] || state.length == 0;
  NSString *agent = GRStr(activity[@"agent"]);
  if (idle) {
    self.pillLabel.text = @"GHOSTroom";
  }
  else {
    NSString *detail = GRStr(activity[@"detail"]);
    NSString *text = agent.length ? [NSString stringWithFormat:@"%@ · %@", agent, GRStr(activity[@"label"])] :
                                    GRStr(activity[@"label"]);
    if (detail.length && ![state isEqualToString:@"failed"]) {
      text = [NSString stringWithFormat:@"%@ — %@", text, detail];
    }
    self.pillLabel.text = text;
  }
  UIColor *color = GRStateColor(state);
  self.pillDot.hidden = idle;
  self.pillDot.backgroundColor = color;
  self.pill.layer.borderColor = [color colorWithAlphaComponent:idle ? 0.35 : 0.8].CGColor;
  self.pillStop.hidden = !GRBool(activity[@"can_stop"]);
  self.pill.accessibilityLabel = [NSString stringWithFormat:@"GHOSTroom: %@", self.pillLabel.text];
}

- (void)stopActive
{
  NSString *taskId = GRStr(GRDict(self.snapshot[@"activity"])[@"task_id"]);
  if (taskId.length) {
    GRSend(@{@"type" : @"stop", @"task_id" : taskId});
  }
}

/* ---- recovery after an interruption ---- */

- (void)renderRecovery
{
  NSDictionary *recovery = GRDict(self.snapshot[@"recovery"]);
  for (UIView *view in self.recoveryBox.arrangedSubviews) {
    [self.recoveryBox removeArrangedSubview:view];
    [view removeFromSuperview];
  }
  if (!recovery) {
    return;
  }
  UIView *card = [[UIView alloc] init];
  card.backgroundColor = [GRAmber() colorWithAlphaComponent:0.12];
  card.layer.cornerRadius = 14;
  card.layer.borderWidth = 1;
  card.layer.borderColor = [GRAmber() colorWithAlphaComponent:0.6].CGColor;
  UILabel *headline = GRLabel(14, UIFontWeightSemibold, GRAmber(), 0);
  headline.text = GRStr(recovery[@"headline"]);
  UILabel *title = GRLabel(15, UIFontWeightSemibold, GRInk(), 2);
  title.text = [NSString stringWithFormat:@"%@ · %@", GRStr(recovery[@"agent"]), GRStr(recovery[@"title"])];
  NSMutableArray *lines = [NSMutableArray array];
  if (GRStr(recovery[@"last_activity"]).length) {
    [lines addObject:[NSString stringWithFormat:@"Last activity: %@ (%@)",
                                                GRStr(recovery[@"last_activity"]),
                                                GRClock(GRNum(recovery[@"last_time"]))]];
  }
  NSDictionary *interrupted = GRDict(recovery[@"interrupted_operation"]);
  if (interrupted) {
    [lines addObject:[NSString stringWithFormat:@"A Blender %@ was running when the app closed.",
                                                GRStr(interrupted[@"operation"])]];
  }
  if (GRBool(recovery[@"mutation_possible"])) {
    [lines addObject:@"The scene may be partly changed — the agent will inspect it before continuing."];
  }
  UILabel *detail = GRLabel(13, UIFontWeightRegular, GRMuted(), 0);
  detail.text = [lines componentsJoinedByString:@"\n"];
  UIButton *resume = GRTintedButton(@"Continue", @"play.fill", GRMint());
  [resume addAction:[UIAction actionWithHandler:^(UIAction *a) {
            GRSend(@{@"type" : @"recovery", @"action" : @"continue"});
          }]
      forControlEvents:UIControlEventPrimaryActionTriggered];
  UIButton *explain = GRTintedButton(@"What happened?", @"questionmark", GRViolet());
  [explain addAction:[UIAction actionWithHandler:^(UIAction *a) {
             GRSend(@{@"type" : @"recovery", @"action" : @"explain"});
           }]
      forControlEvents:UIControlEventPrimaryActionTriggered];
  UIButton *dismiss = GRPlainButton(@"Dismiss", nil, GRMuted());
  [dismiss addAction:[UIAction actionWithHandler:^(UIAction *a) {
             GRSend(@{@"type" : @"recovery", @"action" : @"dismiss"});
           }]
      forControlEvents:UIControlEventPrimaryActionTriggered];
  NSMutableArray *views = [NSMutableArray arrayWithArray:@[ headline, title, detail ]];
  UIImage *evidence = GRImageAt(GRStr(recovery[@"evidence_path"]));
  if (evidence) {
    UIImageView *thumb = [[UIImageView alloc] initWithImage:evidence];
    thumb.contentMode = UIViewContentModeScaleAspectFill;
    thumb.clipsToBounds = YES;
    thumb.layer.cornerRadius = 10;
    [thumb.heightAnchor constraintEqualToConstant:110].active = YES;
    [views addObject:thumb];
  }
  [views addObject:GRStack(@[ resume, explain, GRSpacer(), dismiss ], UILayoutConstraintAxisHorizontal, 8)];
  UIStackView *stack = GRStack(views, UILayoutConstraintAxisVertical, 6);
  stack.translatesAutoresizingMaskIntoConstraints = NO;
  [card addSubview:stack];
  [NSLayoutConstraint activateConstraints:@[
    [stack.leadingAnchor constraintEqualToAnchor:card.leadingAnchor constant:12],
    [stack.trailingAnchor constraintEqualToAnchor:card.trailingAnchor constant:-12],
    [stack.topAnchor constraintEqualToAnchor:card.topAnchor constant:10],
    [stack.bottomAnchor constraintEqualToAnchor:card.bottomAnchor constant:-10],
  ]];
  [self.recoveryBox addArrangedSubview:card];
}

/* ---- context attachments ---- */

- (UIMenu *)attachMenu
{
  NSArray *blender = @[
    @[ @"Current scene", @"cube.transparent", @"scene" ],
    @[ @"Selected objects", @"cursorarrow.rays", @"selection" ],
    @[ @"Viewport capture", @"camera.viewfinder", @"viewport" ],
    @[ @"Render result", @"photo", @"render" ],
  ];
  NSMutableArray *fromBlender = [NSMutableArray array];
  for (NSArray *entry in blender) {
    NSString *kind = entry[2];
    [fromBlender addObject:[UIAction actionWithTitle:entry[0]
                                               image:GRSymbol(entry[1], 15, UIFontWeightMedium)
                                          identifier:nil
                                             handler:^(UIAction *a) {
                                               GRSend(@{@"type" : @"attach", @"kind" : kind});
                                             }]];
  }
  __weak GRController *weakSelf = self;
  UIAction *photo = [UIAction actionWithTitle:@"Image or reference…"
                                        image:GRSymbol(@"photo.on.rectangle.angled", 15, UIFontWeightMedium)
                                   identifier:nil
                                      handler:^(UIAction *a) {
                                        [weakSelf pickPhoto];
                                      }];
  UIAction *file = [UIAction actionWithTitle:@"File…"
                                       image:GRSymbol(@"doc", 15, UIFontWeightMedium)
                                  identifier:nil
                                     handler:^(UIAction *a) {
                                       [weakSelf pickFile];
                                     }];
  UIMenu *blenderMenu = [UIMenu menuWithTitle:@"From Blender"
                                        image:nil
                                   identifier:nil
                                      options:UIMenuOptionsDisplayInline
                                     children:fromBlender];
  UIMenu *otherMenu = [UIMenu menuWithTitle:@"From iPad"
                                      image:nil
                                 identifier:nil
                                    options:UIMenuOptionsDisplayInline
                                   children:@[ photo, file ]];
  return [UIMenu menuWithTitle:@"Attach context" children:@[ blenderMenu, otherMenu ]];
}

- (void)showAgents
{
  GRAgentsSheet *sheet = [[GRAgentsSheet alloc] init];
  [sheet refresh:GRDict(self.snapshot[@"setup"])];
  UINavigationController *navigation = [[UINavigationController alloc] initWithRootViewController:sheet];
  navigation.modalPresentationStyle = UIModalPresentationFormSheet;
  navigation.overrideUserInterfaceStyle = UIUserInterfaceStyleDark;
  self.agentsSheet = navigation;
  [[self presenter] presentViewController:navigation animated:YES completion:nil];
}

- (UIViewController *)presenter
{
  UIViewController *controller = self.window.rootViewController;
  while (controller.presentedViewController) {
    controller = controller.presentedViewController;
  }
  return controller;
}

- (void)pickPhoto
{
  PHPickerConfiguration *config = [[PHPickerConfiguration alloc] init];
  config.filter = [PHPickerFilter imagesFilter];
  config.selectionLimit = 4;
  PHPickerViewController *picker = [[PHPickerViewController alloc] initWithConfiguration:config];
  picker.delegate = self;
  [[self presenter] presentViewController:picker animated:YES completion:nil];
}

- (void)picker:(PHPickerViewController *)picker didFinishPicking:(NSArray<PHPickerResult *> *)results
{
  [picker dismissViewControllerAnimated:YES completion:nil];
  for (PHPickerResult *result in results) {
    NSItemProvider *provider = result.itemProvider;
    if (![provider canLoadObjectOfClass:[UIImage class]]) {
      continue;
    }
    NSString *name = provider.suggestedName ?: @"Image";
    [provider loadObjectOfClass:[UIImage class]
              completionHandler:^(id<NSItemProviderReading> object, NSError *error) {
                if ([(NSObject *)object isKindOfClass:[UIImage class]]) {
                  [self stageImage:(UIImage *)object name:name kind:@"image"];
                }
              }];
  }
}

/* Downscale on a background queue, then hand the file to GhostBlender. */
- (void)stageImage:(UIImage *)image name:(NSString *)name kind:(NSString *)kind
{
  dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
    CGSize size = image.size;
    CGFloat scale = MIN(1.0, 1600.0 / MAX(size.width, size.height));
    CGSize target = CGSizeMake(MAX(1, floor(size.width * scale)), MAX(1, floor(size.height * scale)));
    UIGraphicsImageRendererFormat *format = [UIGraphicsImageRendererFormat defaultFormat];
    format.scale = 1;
    format.opaque = YES;
    UIGraphicsImageRenderer *renderer = [[UIGraphicsImageRenderer alloc] initWithSize:target format:format];
    UIImage *scaled = [renderer imageWithActions:^(UIGraphicsImageRendererContext *context) {
      [image drawInRect:CGRectMake(0, 0, target.width, target.height)];
    }];
    NSData *jpeg = UIImageJPEGRepresentation(scaled, 0.85);
    CGFloat quality = 0.85;
    while (jpeg.length > 1800000 && quality > 0.3) {
      quality -= 0.15;
      jpeg = UIImageJPEGRepresentation(scaled, quality);
    }
    NSString *path = [NSTemporaryDirectory()
        stringByAppendingPathComponent:[NSString stringWithFormat:@"ghostroom-%@.jpg", GRNewId()]];
    if (![jpeg writeToFile:path atomically:YES]) {
      return;
    }
    dispatch_async(dispatch_get_main_queue(), ^{
      GRSend(@{
        @"type" : @"attach_file",
        @"kind" : kind,
        @"path" : path,
        @"name" : name,
        @"media_type" : @"image/jpeg",
        @"width" : @((NSInteger)target.width),
        @"height" : @((NSInteger)target.height),
      });
    });
  });
}

- (void)pickFile
{
  UIDocumentPickerViewController *picker =
      [[UIDocumentPickerViewController alloc] initForOpeningContentTypes:@[ UTTypeItem ] asCopy:YES];
  picker.delegate = self;
  picker.allowsMultipleSelection = NO;
  [[self presenter] presentViewController:picker animated:YES completion:nil];
}

- (void)documentPicker:(UIDocumentPickerViewController *)controller
    didPickDocumentsAtURLs:(NSArray<NSURL *> *)urls
{
  NSURL *url = urls.firstObject;
  if (!url) {
    return;
  }
  id value = nil;
  [url getResourceValue:&value forKey:NSURLContentTypeKey error:nil];
  UTType *type = [value isKindOfClass:[UTType class]] ? (UTType *)value : nil;
  if (type && [type conformsToType:UTTypeImage]) {
    UIImage *image = [UIImage imageWithContentsOfFile:url.path];
    if (image) {
      [self stageImage:image name:url.lastPathComponent kind:@"image"];
      return;
    }
  }
  GRSend(@{@"type" : @"attach_file", @"kind" : @"file", @"path" : url.path ?: @"",
           @"name" : url.lastPathComponent ?: @"File"});
}

- (void)renderAttachments
{
  for (UIView *view in self.chips.arrangedSubviews) {
    [self.chips removeArrangedSubview:view];
    [view removeFromSuperview];
  }
  NSArray *attachments = GRArr(self.snapshot[@"attachments"]);
  self.chipScroll.hidden = attachments.count == 0;
  for (NSDictionary *attachment in attachments) {
    BOOL failed = [GRStr(attachment[@"state"]) isEqualToString:@"failed"];
    UIView *chip = [[UIView alloc] init];
    chip.backgroundColor = [(failed ? GRCoral() : GRMint()) colorWithAlphaComponent:0.14];
    chip.layer.cornerRadius = 12;
    chip.layer.borderWidth = 1;
    chip.layer.borderColor = [(failed ? GRCoral() : GRMint()) colorWithAlphaComponent:0.45].CGColor;
    UIImageView *thumb = [[UIImageView alloc] init];
    UIImage *image = GRImageAt(GRStr(attachment[@"path"]));
    NSDictionary *symbols = @{@"scene" : @"cube.transparent", @"selection" : @"cursorarrow.rays",
                              @"viewport" : @"camera.viewfinder", @"render" : @"photo",
                              @"image" : @"photo.on.rectangle.angled", @"file" : @"doc"};
    thumb.image = image ?: GRSymbol(symbols[GRStr(attachment[@"kind"])] ?: @"paperclip", 16, UIFontWeightMedium);
    thumb.tintColor = failed ? GRCoral() : GRMint();
    thumb.contentMode = image ? UIViewContentModeScaleAspectFill : UIViewContentModeCenter;
    thumb.clipsToBounds = YES;
    thumb.layer.cornerRadius = 8;
    UILabel *title = GRLabel(12, UIFontWeightSemibold, GRInk(), 1);
    title.text = GRStr(attachment[@"title"]);
    UILabel *subtitle = GRLabel(11, UIFontWeightRegular, failed ? GRCoral() : GRMuted(), 1);
    subtitle.text = GRStr(attachment[@"subtitle"]);
    NSString *identifier = GRStr(attachment[@"id"]);
    UIButton *remove = [UIButton buttonWithType:UIButtonTypeSystem];
    [remove setImage:GRSymbol(@"xmark.circle.fill", 15, UIFontWeightMedium) forState:UIControlStateNormal];
    remove.tintColor = GRMuted();
    remove.accessibilityLabel = @"Remove attachment";
    [remove addAction:[UIAction actionWithHandler:^(UIAction *a) {
              GRSend(@{@"type" : @"remove_attachment", @"id" : identifier});
            }]
        forControlEvents:UIControlEventPrimaryActionTriggered];
    UIStackView *text = GRStack(@[ title, subtitle ], UILayoutConstraintAxisVertical, 0);
    UIStackView *row = GRStack(@[ thumb, text, remove ], UILayoutConstraintAxisHorizontal, 8);
    row.translatesAutoresizingMaskIntoConstraints = NO;
    [chip addSubview:row];
    [NSLayoutConstraint activateConstraints:@[
      [thumb.widthAnchor constraintEqualToConstant:40],
      [thumb.heightAnchor constraintEqualToConstant:40],
      [text.widthAnchor constraintLessThanOrEqualToConstant:170],
      [row.leadingAnchor constraintEqualToAnchor:chip.leadingAnchor constant:6],
      [row.trailingAnchor constraintEqualToAnchor:chip.trailingAnchor constant:-4],
      [row.topAnchor constraintEqualToAnchor:chip.topAnchor constant:4],
      [row.bottomAnchor constraintEqualToAnchor:chip.bottomAnchor constant:-4],
    ]];
    [self.chips addArrangedSubview:chip];
  }
}

/* ---- composing ---- */

- (void)textViewDidChange:(UITextView *)textView
{
  self.placeholder.hidden = textView.text.length > 0;
  CGFloat width = textView.bounds.size.width > 0 ? textView.bounds.size.width : 300;
  CGFloat height = [textView sizeThatFits:CGSizeMake(width, CGFLOAT_MAX)].height;
  CGFloat room = self.window.bounds.size.height - self.keyboardOverlap;
  CGFloat maximum = MAX(90.0, MIN(self.window.bounds.size.height * 0.32, room * 0.3));
  textView.scrollEnabled = height > maximum;
  self.composerHeight.constant = MAX(46, MIN(maximum, height));
}

- (void)sendMessage
{
  NSString *text = [self.composer.text stringByTrimmingCharactersInSet:
                                           [NSCharacterSet whitespaceAndNewlineCharacterSet]];
  if (text.length == 0) {
    return;
  }
  NSMutableArray *attachments = [NSMutableArray array];
  for (NSDictionary *attachment in GRArr(self.snapshot[@"attachments"])) {
    if ([GRStr(attachment[@"state"]) isEqualToString:@"ready"]) {
      [attachments addObject:GRStr(attachment[@"id"])];
    }
  }
  NSDictionary *selected = GRDict(self.snapshot[@"selected"]);
  NSMutableDictionary *command = [@{
    @"type" : @"send",
    @"id" : GRNewId(),
    @"text" : text,
    @"attachments" : attachments,
    @"mode" : GRStr(selected[@"mode"]).length ? GRStr(selected[@"mode"]) : @"do",
    @"role" : GRStr(selected[@"role"]).length ? GRStr(selected[@"role"]) : @"primary",
  } mutableCopy];
  if (GRStr(selected[@"agent_id"]).length) {
    command[@"agent_id"] = GRStr(selected[@"agent_id"]);
  }
  GRSend(command);
  self.composer.text = @"";
  [self textViewDidChange:self.composer];
  UIImpactFeedbackGenerator *haptic = [[UIImpactFeedbackGenerator alloc]
      initWithStyle:UIImpactFeedbackStyleLight];
  [haptic impactOccurred];
}

/* ---- timeline ---- */

- (NSInteger)tableView:(UITableView *)tableView numberOfRowsInSection:(NSInteger)section
{
  return (NSInteger)self.items.count;
}

- (UITableViewCell *)tableView:(UITableView *)tableView cellForRowAtIndexPath:(NSIndexPath *)indexPath
{
  GRItemCell *cell = [tableView dequeueReusableCellWithIdentifier:@"item" forIndexPath:indexPath];
  [cell reset];
  NSDictionary *item = GRDict(self.items[(NSUInteger)indexPath.row]) ?: @{};
  NSString *type = GRStr(item[@"type"]);
  if ([type isEqualToString:@"user"]) {
    [self configureUser:cell item:item];
  }
  else if ([type isEqualToString:@"agent"]) {
    [self configureAgent:cell item:item];
  }
  else if ([type isEqualToString:@"task"] || [type isEqualToString:@"external"]) {
    [self configureTask:cell item:item];
  }
  else if ([type isEqualToString:@"failure"]) {
    [self configureFailure:cell item:item];
  }
  else if ([type isEqualToString:@"note"]) {
    [self configureNote:cell item:item];
  }
  else {
    [self configureSystem:cell item:item];
  }
  return cell;
}

- (UIView *)metaRow:(NSString *)name color:(UIColor *)color time:(double)time tag:(NSString *)tag
{
  UIView *dot = [[UIView alloc] init];
  dot.backgroundColor = color;
  dot.layer.cornerRadius = 4;
  [dot.widthAnchor constraintEqualToConstant:8].active = YES;
  [dot.heightAnchor constraintEqualToConstant:8].active = YES;
  UILabel *who = GRLabel(13, UIFontWeightSemibold, color, 1);
  who.text = name;
  UILabel *when = GRLabel(11, UIFontWeightMedium, GRMuted(), 1);
  when.text = GRClock(time);
  NSMutableArray *views = [NSMutableArray arrayWithArray:@[ dot, who ]];
  if (tag.length) {
    UILabel *chip = GRLabel(11, UIFontWeightSemibold, GRViolet(), 1);
    chip.text = [NSString stringWithFormat:@" %@ ", tag];
    chip.layer.borderColor = [GRViolet() colorWithAlphaComponent:0.5].CGColor;
    chip.layer.borderWidth = 1;
    chip.layer.cornerRadius = 6;
    [views addObject:chip];
  }
  [views addObjectsFromArray:@[ GRSpacer(), when ]];
  return GRStack(views, UILayoutConstraintAxisHorizontal, 6);
}

- (NSString *)modeTag:(NSDictionary *)item
{
  NSMutableArray *tags = [NSMutableArray array];
  NSDictionary *modes = @{@"with_me" : @"With me", @"teach" : @"Teach", @"explain" : @"Explain",
                          @"review_my_work" : @"Review my work"};
  if (modes[GRStr(item[@"mode"])]) {
    [tags addObject:modes[GRStr(item[@"mode"])]];
  }
  NSString *role = GRStr(item[@"role"]);
  if (role.length && ![role isEqualToString:@"primary"]) {
    [tags addObject:role.capitalizedString];
  }
  if (GRBool(item[@"redirect"])) {
    [tags addObject:@"Redirect"];
  }
  return [tags componentsJoinedByString:@" · "];
}

- (void)configureUser:(GRItemCell *)cell item:(NSDictionary *)item
{
  cell.leading.constant = 56;
  cell.card.backgroundColor = [GRMint() colorWithAlphaComponent:0.16];
  cell.card.layer.borderWidth = 1;
  cell.card.layer.borderColor = [GRMint() colorWithAlphaComponent:0.35].CGColor;
  cell.card.alpha = GRBool(item[@"pending"]) ? 0.6 : 1.0;
  NSString *agent = GRStr(item[@"agent"]);
  NSString *name = agent.length ? [NSString stringWithFormat:@"You → %@", agent] : @"You";
  [cell.stack addArrangedSubview:[self metaRow:name
                                         color:GRMint()
                                          time:GRNum(item[@"time"])
                                           tag:[self modeTag:item]]];
  UITextView *body = GRTextBody();
  body.attributedText = [[NSAttributedString alloc]
      initWithString:GRStr(item[@"text"])
          attributes:@{NSFontAttributeName : GRFont(16, UIFontWeightRegular),
                       NSForegroundColorAttributeName : GRInk()}];
  [cell.stack addArrangedSubview:body];
  NSArray *context = GRArr(item[@"context"]);
  if (context.count || GRBool(item[@"pending"])) {
    UILabel *extra = GRLabel(12, UIFontWeightMedium, GRMuted(), 0);
    NSMutableArray *parts = [NSMutableArray array];
    if (context.count) {
      [parts addObject:[@"📎 " stringByAppendingString:[context componentsJoinedByString:@" · "]]];
    }
    if (GRBool(item[@"pending"])) {
      [parts addObject:@"Sending…"];
    }
    extra.text = [parts componentsJoinedByString:@"   "];
    [cell.stack addArrangedSubview:extra];
  }
}

- (void)configureAgent:(GRItemCell *)cell item:(NSDictionary *)item
{
  cell.trailing.constant = -28;
  [cell.stack addArrangedSubview:[self metaRow:GRStr(item[@"agent"])
                                         color:GRViolet()
                                          time:GRNum(item[@"time"])
                                           tag:nil]];
  UITextView *body = GRTextBody();
  body.attributedText = GRMarkdown(GRStr(item[@"text"]), 16, GRInk());
  [cell.stack addArrangedSubview:body];
}

- (void)configureFailure:(GRItemCell *)cell item:(NSDictionary *)item
{
  cell.card.backgroundColor = [GRCoral() colorWithAlphaComponent:0.12];
  cell.card.layer.borderWidth = 1;
  cell.card.layer.borderColor = [GRCoral() colorWithAlphaComponent:0.55].CGColor;
  [cell.stack addArrangedSubview:[self metaRow:GRStr(item[@"agent"])
                                         color:GRCoral()
                                          time:GRNum(item[@"time"])
                                           tag:nil]];
  UITextView *body = GRTextBody();
  body.attributedText = [[NSAttributedString alloc]
      initWithString:GRStr(item[@"text"])
          attributes:@{NSFontAttributeName : GRFont(15, UIFontWeightMedium),
                       NSForegroundColorAttributeName : GRInk()}];
  [cell.stack addArrangedSubview:body];
  NSDictionary *failure = GRDict(item[@"failure"]);
  if (failure) {
    UILabel *code = GRLabel(12, UIFontWeightMedium, GRMuted(), 0);
    NSString *layer = GRStr(failure[@"layer"]);
    NSDictionary *layers = @{@"agent_provider" : @"agent provider", @"router" : @"agent router",
                             @"relay" : @"relay", @"bridge" : @"GhostBlender bridge", @"blender" : @"Blender",
                             @"tool" : @"Blender tool"};
    code.text = [NSString stringWithFormat:@"%@ · in the %@%@",
                                           [GRStr(failure[@"code"]) stringByReplacingOccurrencesOfString:@"_"
                                                                                              withString:@" "],
                                           layers[layer] ?: layer,
                                           GRBool(failure[@"mutation_possible"]) ?
                                               @"\nBlender may have been changed — inspect before retrying." :
                                               @""];
    [cell.stack addArrangedSubview:code];
  }
}

- (void)configureNote:(GRItemCell *)cell item:(NSDictionary *)item
{
  cell.card.layer.borderWidth = 1;
  cell.card.layer.borderColor = [GRViolet() colorWithAlphaComponent:0.5].CGColor;
  NSString *category = GRStr(item[@"category"]).capitalizedString;
  [cell.stack addArrangedSubview:[self metaRow:GRStr(item[@"agent"])
                                         color:GRViolet()
                                          time:GRNum(item[@"time"])
                                           tag:category]];
  UITextView *body = GRTextBody();
  body.attributedText = GRMarkdown(GRStr(item[@"text"]), 15, GRInk());
  [cell.stack addArrangedSubview:body];
  NSString *rationale = GRStr(item[@"rationale"]);
  if (rationale.length) {
    UILabel *why = GRLabel(13, UIFontWeightRegular, GRMuted(), 0);
    why.text = [@"Why: " stringByAppendingString:rationale];
    [cell.stack addArrangedSubview:why];
  }
  NSString *target = GRStr(item[@"forward_name"]);
  if (target.length) {
    NSString *identifier = GRStr(item[@"id"]);
    UIButton *forward = GRTintedButton([NSString stringWithFormat:@"Hand to %@", target],
                                       @"arrowshape.turn.up.right", GRViolet());
    [forward addAction:[UIAction actionWithHandler:^(UIAction *a) {
               GRSend(@{@"type" : @"forward_note", @"item_id" : identifier});
             }]
        forControlEvents:UIControlEventPrimaryActionTriggered];
    [cell.stack addArrangedSubview:GRStack(@[ forward, GRSpacer() ], UILayoutConstraintAxisHorizontal, 0)];
  }
}

- (void)configureSystem:(GRItemCell *)cell item:(NSDictionary *)item
{
  cell.card.backgroundColor = [UIColor clearColor];
  UILabel *label = GRLabel(12, UIFontWeightMedium, GRMuted(), 0);
  label.textAlignment = NSTextAlignmentCenter;
  label.text = [NSString stringWithFormat:@"%@ · %@", GRStr(item[@"text"]), GRClock(GRNum(item[@"time"]))];
  [cell.stack addArrangedSubview:label];
}

/* A task: broad evolving phases derived from real tool calls, evidence, stop. */
- (void)configureTask:(GRItemCell *)cell item:(NSDictionary *)item
{
  NSString *state = GRStr(item[@"state"]);
  BOOL external = [GRStr(item[@"type"]) isEqualToString:@"external"];
  UIColor *color = GRStateColor(state);
  cell.card.layer.borderWidth = 1;
  cell.card.layer.borderColor = [color colorWithAlphaComponent:0.45].CGColor;

  UIImageView *icon = [[UIImageView alloc] initWithImage:GRSymbol(GRStateSymbol(state), 13, UIFontWeightBold)];
  icon.tintColor = color;
  UILabel *stateLabel = GRLabel(12, UIFontWeightBold, color, 1);
  stateLabel.text = external ? @"External" : [GRTaskStateLabel(state) uppercaseString];
  UILabel *agent = GRLabel(13, UIFontWeightSemibold, GRInk(), 1);
  agent.text = GRStr(item[@"agent"]);
  UILabel *elapsed = GRLabel(11, UIFontWeightMedium, GRMuted(), 1);
  BOOL live = [state isEqualToString:@"running"] || [state isEqualToString:@"stopping"];
  elapsed.text = live ? GRElapsed(GRNum(item[@"time"])) : GRClock(GRNum(item[@"updated"]));
  NSMutableArray *headerViews = [NSMutableArray arrayWithArray:@[ icon, stateLabel, agent ]];
  NSString *tag = [self modeTag:item];
  if (tag.length) {
    UILabel *chip = GRLabel(11, UIFontWeightSemibold, GRViolet(), 1);
    chip.text = tag;
    [headerViews addObject:chip];
  }
  [headerViews addObjectsFromArray:@[ GRSpacer(), elapsed ]];
  [cell.stack addArrangedSubview:GRStack(headerViews, UILayoutConstraintAxisHorizontal, 6)];

  UILabel *title = GRLabel(16, UIFontWeightSemibold, GRInk(), 2);
  title.text = GRStr(item[@"title"]);
  [cell.stack addArrangedSubview:title];

  NSString *narration = GRStr(item[@"narration"]);
  if (narration.length && live) {
    UILabel *said = GRLabel(13, UIFontWeightRegular, GRMuted(), 3);
    said.font = [UIFont italicSystemFontOfSize:13];
    said.text = narration;
    [cell.stack addArrangedSubview:said];
  }

  NSArray *phases = GRArr(item[@"phases"]);
  NSString *identifier = GRStr(item[@"id"]);
  BOOL expanded = [self.expanded containsObject:identifier];
  NSUInteger visible = expanded ? phases.count : MIN((NSUInteger)4, phases.count);
  for (NSUInteger i = phases.count - visible; i < phases.count; i++) {
    NSDictionary *phase = GRDict(phases[i]);
    NSString *phaseState = GRStr(phase[@"state"]);
    UIView *marker;
    if ([phaseState isEqualToString:@"running"]) {
      UIActivityIndicatorView *spinner = [[UIActivityIndicatorView alloc]
          initWithActivityIndicatorStyle:UIActivityIndicatorViewStyleMedium];
      spinner.color = GRMint();
      spinner.transform = CGAffineTransformMakeScale(0.7, 0.7);
      [spinner startAnimating];
      marker = spinner;
    }
    else {
      NSString *symbol = [phaseState isEqualToString:@"failed"] ? @"xmark.circle.fill" :
                         ([phaseState isEqualToString:@"uncertain"] ? @"exclamationmark.circle.fill" :
                          ([phaseState isEqualToString:@"cancelled"] ? @"minus.circle" :
                                                                       @"checkmark.circle.fill"));
      UIImageView *mark = [[UIImageView alloc] initWithImage:GRSymbol(symbol, 13, UIFontWeightSemibold)];
      mark.tintColor = [phaseState isEqualToString:@"done"] ? [GRMint() colorWithAlphaComponent:0.8] :
                                                              GRStateColor(phaseState);
      marker = mark;
    }
    [marker.widthAnchor constraintEqualToConstant:18].active = YES;
    UILabel *label = GRLabel(14, UIFontWeightRegular, GRInk(), 2);
    label.text = GRStr(phase[@"label"]);
    UILabel *count = GRLabel(11, UIFontWeightMedium, GRMuted(), 1);
    NSInteger n = (NSInteger)GRNum(phase[@"count"]);
    count.text = n > 1 ? [NSString stringWithFormat:@"×%ld", (long)n] : @"";
    [cell.stack addArrangedSubview:GRStack(@[ marker, label, GRSpacer(), count ],
                                           UILayoutConstraintAxisHorizontal,
                                           8)];
    NSDictionary *failure = GRDict(phase[@"failure"]);
    if (failure && ![phaseState isEqualToString:@"done"]) {
      UILabel *problem = GRLabel(12, UIFontWeightRegular, GRCoral(), 3);
      problem.text = GRStr(failure[@"message"]);
      [cell.stack addArrangedSubview:problem];
    }
  }
  if (phases.count > 4) {
    UIButton *more = GRPlainButton(expanded ? @"Show recent steps" :
                                              [NSString stringWithFormat:@"Show all %lu phases · %ld operations",
                                                                         (unsigned long)phases.count,
                                                                         (long)GRNum(item[@"operation_count"])],
                                   expanded ? @"chevron.up" : @"chevron.down",
                                   GRMuted());
    more.contentHorizontalAlignment = UIControlContentHorizontalAlignmentLeading;
    [more addAction:[UIAction actionWithHandler:^(UIAction *a) {
            if ([self.expanded containsObject:identifier]) {
              [self.expanded removeObject:identifier];
            }
            else {
              [self.expanded addObject:identifier];
            }
            [self.table reloadData];
          }]
        forControlEvents:UIControlEventPrimaryActionTriggered];
    [cell.stack addArrangedSubview:more];
  }

  NSArray *evidence = GRArr(item[@"evidence"]);
  if (evidence.count) {
    UIScrollView *strip = [[UIScrollView alloc] init];
    strip.showsHorizontalScrollIndicator = NO;
    UIStackView *thumbs = GRStack(@[], UILayoutConstraintAxisHorizontal, 8);
    thumbs.translatesAutoresizingMaskIntoConstraints = NO;
    [strip addSubview:thumbs];
    for (NSDictionary *proof in evidence) {
      [thumbs addArrangedSubview:[self evidenceThumb:GRDict(proof)]];
    }
    [NSLayoutConstraint activateConstraints:@[
      [strip.heightAnchor constraintEqualToConstant:96],
      [thumbs.leadingAnchor constraintEqualToAnchor:strip.contentLayoutGuide.leadingAnchor],
      [thumbs.trailingAnchor constraintEqualToAnchor:strip.contentLayoutGuide.trailingAnchor],
      [thumbs.topAnchor constraintEqualToAnchor:strip.contentLayoutGuide.topAnchor],
      [thumbs.bottomAnchor constraintEqualToAnchor:strip.contentLayoutGuide.bottomAnchor],
      [thumbs.heightAnchor constraintEqualToAnchor:strip.frameLayoutGuide.heightAnchor],
    ]];
    [cell.stack addArrangedSubview:strip];
  }

  if (GRBool(item[@"mutation_possible"]) &&
      ([state isEqualToString:@"stopped"] || [state isEqualToString:@"failed"] ||
       [state isEqualToString:@"uncertain"]))
  {
    UILabel *warning = GRLabel(12, UIFontWeightMedium, GRAmber(), 0);
    warning.text = @"Blender may have been changed before this ended. Ask the agent to inspect before continuing.";
    [cell.stack addArrangedSubview:warning];
  }
  NSInteger redirects = (NSInteger)GRNum(item[@"redirects"]);
  if (redirects > 0) {
    UILabel *redirected = GRLabel(12, UIFontWeightMedium, GRViolet(), 1);
    redirected.text = [NSString stringWithFormat:@"Redirected %ld time%@ while working",
                                                 (long)redirects,
                                                 redirects == 1 ? @"" : @"s"];
    [cell.stack addArrangedSubview:redirected];
  }
  if (GRBool(item[@"can_stop"])) {
    NSString *taskId = GRStr(item[@"task_id"]);
    UIButton *stop = GRTintedButton([state isEqualToString:@"queued"] ? @"Cancel" : @"Stop", @"stop.fill",
                                    GRCoral());
    [stop addAction:[UIAction actionWithHandler:^(UIAction *a) {
            GRSend(@{@"type" : @"stop", @"task_id" : taskId});
          }]
        forControlEvents:UIControlEventPrimaryActionTriggered];
    [cell.stack addArrangedSubview:GRStack(@[ GRSpacer(), stop ], UILayoutConstraintAxisHorizontal, 0)];
  }
}

- (UIView *)evidenceThumb:(NSDictionary *)proof
{
  UIButton *button = [UIButton buttonWithType:UIButtonTypeCustom];
  button.layer.cornerRadius = 10;
  button.layer.cornerCurve = kCACornerCurveContinuous;
  button.clipsToBounds = YES;
  button.backgroundColor = [UIColor colorWithWhite:1 alpha:0.06];
  button.pointerInteractionEnabled = YES;
  NSString *path = GRStr(proof[@"path"]);
  NSString *label = GRStr(proof[@"label"]);
  UIImage *image = GRImageAt(path);
  if (image) {
    [button setImage:image forState:UIControlStateNormal];
    button.imageView.contentMode = UIViewContentModeScaleAspectFill;
    button.contentHorizontalAlignment = UIControlContentHorizontalAlignmentFill;
    button.contentVerticalAlignment = UIControlContentVerticalAlignmentFill;
    [button addAction:[UIAction actionWithHandler:^(UIAction *a) {
              [self showImage:image caption:label];
            }]
        forControlEvents:UIControlEventPrimaryActionTriggered];
  }
  else {
    [button setImage:GRSymbol(@"photo", 20, UIFontWeightMedium) forState:UIControlStateNormal];
    button.tintColor = GRMuted();
    NSString *artifact = GRStr(proof[@"artifact_id"]);
    if (artifact.length && ![self.requestedFetches containsObject:artifact]) {
      [self.requestedFetches addObject:artifact];
      GRSend(@{@"type" : @"fetch", @"artifact_id" : artifact});
    }
  }
  button.accessibilityLabel = [@"Evidence: " stringByAppendingString:label];
  [NSLayoutConstraint activateConstraints:@[
    [button.widthAnchor constraintEqualToConstant:140],
    [button.heightAnchor constraintEqualToConstant:96],
  ]];
  return button;
}

- (void)showImage:(UIImage *)image caption:(NSString *)caption
{
  GRImageViewer *viewer = [[GRImageViewer alloc] init];
  viewer.image = image;
  viewer.caption = caption;
  viewer.modalPresentationStyle = UIModalPresentationOverFullScreen;
  viewer.modalTransitionStyle = UIModalTransitionStyleCrossDissolve;
  [[self presenter] presentViewController:viewer animated:YES completion:nil];
}

- (UIContextMenuConfiguration *)tableView:(UITableView *)tableView
    contextMenuConfigurationForRowAtIndexPath:(NSIndexPath *)indexPath
                                        point:(CGPoint)point
{
  NSDictionary *item = GRDict(self.items[(NSUInteger)indexPath.row]);
  NSString *text = GRStr(item[@"text"]).length ? GRStr(item[@"text"]) : GRStr(item[@"title"]);
  if (!text.length) {
    return nil;
  }
  return [UIContextMenuConfiguration
      configurationWithIdentifier:nil
                  previewProvider:nil
                   actionProvider:^UIMenu *(NSArray<UIMenuElement *> *suggested) {
                     UIAction *copy = [UIAction actionWithTitle:@"Copy"
                                                          image:GRSymbol(@"doc.on.doc", 15, UIFontWeightMedium)
                                                     identifier:nil
                                                        handler:^(UIAction *a) {
                                                          [UIPasteboard generalPasteboard].string = text;
                                                        }];
                     UIAction *quote = [UIAction actionWithTitle:@"Reply quoting this"
                                                           image:GRSymbol(@"arrowshape.turn.up.left", 15,
                                                                          UIFontWeightMedium)
                                                      identifier:nil
                                                         handler:^(UIAction *a) {
                                                           NSString *quoted = [NSString
                                                               stringWithFormat:@"> %@\n\n",
                                                                                [text substringToIndex:MIN(
                                                                                          (NSUInteger)400,
                                                                                          text.length)]];
                                                           self.composer.text = [quoted
                                                               stringByAppendingString:self.composer.text];
                                                           [self textViewDidChange:self.composer];
                                                           [self.composer becomeFirstResponder];
                                                         }];
                     return [UIMenu menuWithTitle:@"" children:@[ copy, quote ]];
                   }];
}

@end

/* ------------------------------------------------------------------------ Python bridge */

#ifdef GHOSTROOM_HARNESS
/* Simulator validation harness (agent/native/harness): the same controller, no Python. */
extern "C" void ghostroom_harness_update(NSString *json)
{
  [[GRController shared] applySnapshot:json];
}

extern "C" NSArray<NSString *> *ghostroom_harness_take(void)
{
  NSMutableArray *outbox = GROutbox();
  @synchronized(outbox) {
    NSArray *commands = [outbox copy];
    [outbox removeAllObjects];
    return commands;
  }
}

extern "C" id ghostroom_harness_controller(void)
{
  return [GRController shared];
}
#else

extern "C" PyObject *ghostroom_py_update(PyObject * /*self*/, PyObject *args)
{
  const char *json;
  Py_ssize_t length;
  if (!PyArg_ParseTuple(args, "s#", &json, &length)) {
    return nullptr;
  }
  if (length > 8 * 1024 * 1024) {
    return PyErr_Format(PyExc_ValueError, "snapshot_too_large");
  }
  NSString *snapshot = [[NSString alloc] initWithBytes:json
                                                length:(NSUInteger)length
                                              encoding:NSUTF8StringEncoding];
  if (!snapshot) {
    return PyErr_Format(PyExc_ValueError, "invalid_utf8");
  }
  /* Never re-enter UIKit layout from inside Blender's timer: apply on the next run-loop turn. */
  dispatch_async(dispatch_get_main_queue(), ^{
    [[GRController shared] applySnapshot:snapshot];
  });
  Py_RETURN_NONE;
}

extern "C" PyObject *ghostroom_py_take(PyObject * /*self*/, PyObject * /*args*/)
{
  NSArray<NSString *> *commands;
  NSMutableArray *outbox = GROutbox();
  @synchronized(outbox) {
    commands = [outbox copy];
    [outbox removeAllObjects];
  }
  PyObject *list = PyList_New((Py_ssize_t)commands.count);
  if (!list) {
    return nullptr;
  }
  Py_ssize_t index = 0;
  for (NSString *command in commands) {
    PyObject *value = PyUnicode_FromString(command.UTF8String);
    if (!value) {
      Py_DECREF(list);
      return nullptr;
    }
    PyList_SET_ITEM(list, index++, value);
  }
  return list;
}
#endif /* GHOSTROOM_HARNESS */
