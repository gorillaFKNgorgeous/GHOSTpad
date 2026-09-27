/* SPDX-License-Identifier: GPL-2.0-or-later
 * Native network I/O only. No completion callback may enter Python or Blender. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <cstring>
#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>
#include <os/proc.h>
#include <mach/mach.h>

static const NSUInteger GB_LIMIT = 3 * 1024 * 1024;

@interface GBTransport : NSObject <NSURLSessionDataDelegate>
@property(nonatomic, strong) NSURLSession *session;
@property(nonatomic, strong) NSURLSessionDataTask *task;
@property(nonatomic, strong) NSMutableData *data;
@property(nonatomic, strong) NSDictionary *result;
@property(nonatomic) NSInteger status;
- (void)cancel;
@end

@implementation GBTransport
- (instancetype)init
{
  if ((self = [super init])) {
    NSURLSessionConfiguration *config = [NSURLSessionConfiguration ephemeralSessionConfiguration];
    config.HTTPCookieStorage = nil;
    config.URLCache = nil;
    config.HTTPShouldSetCookies = NO;
    config.timeoutIntervalForRequest = 20;
    config.timeoutIntervalForResource = 25;
    config.waitsForConnectivity = NO;
    NSOperationQueue *queue = [[NSOperationQueue alloc] init];
    queue.maxConcurrentOperationCount = 1;
    self.session = [NSURLSession sessionWithConfiguration:config delegate:self delegateQueue:queue];
  }
  return self;
}
- (void)cancel
{
  @synchronized(self) {
    [self.task cancel];
    self.task = nil;
    self.data = nil;
    self.result = nil;
  }
}
- (void)URLSession:(NSURLSession *)session task:(NSURLSessionTask *)task
 willPerformHTTPRedirection:(NSHTTPURLResponse *)response newRequest:(NSURLRequest *)request
 completionHandler:(void (^)(NSURLRequest *))completionHandler
{
  /* Never forward a device credential to a redirected origin or path. */
  completionHandler(nil);
}
- (void)URLSession:(NSURLSession *)session dataTask:(NSURLSessionDataTask *)task
 didReceiveResponse:(NSURLResponse *)response
 completionHandler:(void (^)(NSURLSessionResponseDisposition))completionHandler
{
  @synchronized(self) {
    if (task != self.task) { completionHandler(NSURLSessionResponseCancel); return; }
    self.status = [(NSHTTPURLResponse *)response statusCode];
    if (response.expectedContentLength > (int64_t)GB_LIMIT) {
      self.result = @{@"error": @"response_too_large"};
      self.task = nil;
      completionHandler(NSURLSessionResponseCancel);
      return;
    }
  }
  completionHandler(NSURLSessionResponseAllow);
}
- (void)URLSession:(NSURLSession *)session dataTask:(NSURLSessionDataTask *)task
 didReceiveData:(NSData *)data
{
  @synchronized(self) {
    if (task != self.task) return;
    if (self.data.length + data.length > GB_LIMIT) {
      self.result = @{@"error": @"response_too_large"};
      [self.task cancel];
      self.task = nil;
      self.data = nil;
      return;
    }
    [self.data appendData:data];
  }
}
- (void)URLSession:(NSURLSession *)session task:(NSURLSessionTask *)task
 didCompleteWithError:(NSError *)error
{
  @synchronized(self) {
    if (task != self.task) return;
    if (error) {
      /* Error text may contain the URL. Return only domain/code, never credentials. */
      self.result = @{@"error": [NSString stringWithFormat:@"%@: %ld", error.domain,
                                                        (long)error.code]};
    }
    else {
      NSString *body = [[NSString alloc] initWithData:self.data encoding:NSUTF8StringEncoding];
      self.result = body ? @{@"status": @(self.status), @"body": body} :
                           @{@"error": @"invalid_utf8"};
    }
    self.task = nil;
    self.data = nil;
  }
}
@end

static GBTransport *transport;

static PyObject *gb_request(PyObject *, PyObject *args)
{
  const char *url, *token, *body;
  Py_ssize_t body_len;
  if (!PyArg_ParseTuple(args, "sss#", &url, &token, &body, &body_len)) return nullptr;
  if (body_len > (Py_ssize_t)GB_LIMIT || strlen(token) > 512) {
    return PyErr_Format(PyExc_ValueError, "request_too_large");
  }
  for (const char *p = token; *p; ++p) {
    if (!( (*p >= 'A' && *p <= 'Z') || (*p >= 'a' && *p <= 'z') ||
           (*p >= '0' && *p <= '9') || *p == '-' || *p == '_' )) {
      return PyErr_Format(PyExc_ValueError, "invalid_device_token");
    }
  }
  @autoreleasepool {
    NSURL *target = [NSURL URLWithString:[NSString stringWithUTF8String:url]];
    NSString *credential = [NSString stringWithUTF8String:token];
    if (!target || ![target.scheme isEqualToString:@"https"] || !target.host.length ||
        target.user || target.password || target.query || target.fragment ||
        [credential rangeOfCharacterFromSet:[NSCharacterSet newlineCharacterSet]].location != NSNotFound)
    {
      return PyErr_Format(PyExc_ValueError, "HTTPS URL and valid token required");
    }
    if (!transport) transport = [[GBTransport alloc] init];
    @synchronized(transport) {
      if (transport.task || transport.result) {
        return PyErr_Format(PyExc_RuntimeError, "transport_busy");
      }
      NSMutableURLRequest *req = [NSMutableURLRequest requestWithURL:target];
      req.HTTPMethod = @"POST";
      [req setValue:@"application/json" forHTTPHeaderField:@"Content-Type"];
      [req setValue:[@"Bearer " stringByAppendingString:credential] forHTTPHeaderField:@"Authorization"];
      req.HTTPBody = [NSData dataWithBytes:body length:(NSUInteger)body_len];
      transport.data = [NSMutableData data];
      transport.task = [transport.session dataTaskWithRequest:req];
      [transport.task resume];
    }
  }
  Py_RETURN_NONE;
}

static PyObject *gb_poll(PyObject *, PyObject *)
{
  if (!transport) Py_RETURN_NONE;
  @autoreleasepool {
    NSDictionary *result;
    @synchronized(transport) { result = transport.result; transport.result = nil; }
    if (!result) Py_RETURN_NONE;
    if (result[@"error"]) return Py_BuildValue("{s:s}", "error", [result[@"error"] UTF8String]);
    return Py_BuildValue("{s:l,s:s}", "status", [result[@"status"] longValue],
                         "body", [result[@"body"] UTF8String]);
  }
}

static PyObject *gb_cancel(PyObject *, PyObject *)
{
  [transport cancel];
  Py_RETURN_NONE;
}

static PyObject *gb_status(PyObject *, PyObject *)
{
  task_vm_info_data_t vm = {};
  mach_msg_type_number_t count = TASK_VM_INFO_COUNT;
  const bool ok = task_info(mach_task_self(), TASK_VM_INFO, (task_info_t)&vm, &count) == KERN_SUCCESS;
  return Py_BuildValue("{s:O,s:K,s:K}",
    "foreground", [UIApplication sharedApplication].applicationState == UIApplicationStateActive ? Py_True : Py_False,
    "available_memory_bytes", (unsigned long long)os_proc_available_memory(),
    "physical_footprint_bytes", ok ? (unsigned long long)vm.phys_footprint : 0ULL);
}

/* GHOSTroom native workspace (ghostroom_ui.mm). */
extern "C" PyObject *ghostroom_py_update(PyObject *, PyObject *);
extern "C" PyObject *ghostroom_py_take(PyObject *, PyObject *);
extern "C" PyObject *ghostroom_py_export(PyObject *, PyObject *);

static PyMethodDef methods[] = {
    {"request", gb_request, METH_VARARGS, "Start one authenticated asynchronous HTTPS POST."},
    {"poll", gb_poll, METH_NOARGS, "Take a completed response, or None. Never blocks."},
    {"cancel", gb_cancel, METH_NOARGS, "Cancel I/O and discard its response."},
    {"status", gb_status, METH_NOARGS, "Foreground state and measured process memory."},
    {"room_update", ghostroom_py_update, METH_VARARGS, "Render a GHOSTroom snapshot (JSON) natively."},
    {"room_take", ghostroom_py_take, METH_NOARGS, "Take pending GHOSTroom UI commands (JSON strings)."},
    {"export_files", ghostroom_py_export, METH_VARARGS, "Offer files to move into Files (JSON request)."},
    {nullptr, nullptr, 0, nullptr},
};
static PyModuleDef module = {PyModuleDef_HEAD_INIT, "_ghostbridge_transport", nullptr, -1, methods,
                            nullptr, nullptr, nullptr, nullptr};
extern "C" PyObject *PyInit__ghostbridge_transport(void)
{
  return PyModule_Create(&module);
}
