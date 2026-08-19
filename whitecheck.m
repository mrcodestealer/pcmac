// whitecheck — measure how "white" (blank) a window looks, and map Chrome windows to CGWindowIDs.
//
//   whitecheck --list-chrome                    list Chrome windows: cgid + bounds  (no TCC grant needed)
//   whitecheck --window <cgid> [--crop-top pt]  capture just that window, report whiteness
//   whitecheck --rect x,y,w,h                   capture a screen rect, report whiteness
//
// Exit codes: 0 ok, 2 bad usage, 3 capture failed (usually missing Screen Recording permission).
//
// Build: clang -fobjc-arc -O2 -framework Foundation -framework CoreGraphics \
//              -framework ScreenCaptureKit -framework CoreMedia -o bin/whitecheck whitecheck.m

#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>

static void emit(NSDictionary *obj) {
    NSData *d = [NSJSONSerialization dataWithJSONObject:obj options:NSJSONWritingSortedKeys error:nil];
    if (d) fprintf(stdout, "%s\n", [[[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding] UTF8String]);
}

static void die(NSString *msg, int code) {
    emit(@{@"error": msg ?: @"unknown"});
    exit(code);
}

#pragma mark - window listing

static NSArray *listChromeWindows(void) {
    CFArrayRef raw = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID);
    NSMutableArray *out = [NSMutableArray array];
    if (!raw) return out;
    for (NSDictionary *w in (NSArray *)CFBridgingRelease(raw)) {
        NSString *owner = w[(id)kCGWindowOwnerName];
        if (![owner containsString:@"Chrome"]) continue;
        if ([w[(id)kCGWindowLayer] intValue] != 0) continue;
        NSDictionary *b = w[(id)kCGWindowBounds];
        int ww = [b[@"Width"] intValue], hh = [b[@"Height"] intValue];
        if (ww < 200 || hh < 200) continue;
        [out addObject:@{
            @"cgid":  w[(id)kCGWindowNumber] ?: @0,
            @"owner": owner ?: @"",
            @"x": @([b[@"X"] intValue]), @"y": @([b[@"Y"] intValue]),
            @"w": @(ww), @"h": @(hh),
            @"title": w[(id)kCGWindowName] ?: @"",
        }];
    }
    return out;
}

#pragma mark - pixel stats

typedef struct { double whiteFrac, brightFrac, meanLuma; long sampled; } Stats;

static BOOL measureImage(CGImageRef img, int cropTopPx, Stats *st) {
    size_t w = CGImageGetWidth(img), h = CGImageGetHeight(img);
    if (w == 0 || h == 0) return NO;
    int top = cropTopPx < 0 ? 0 : cropTopPx;
    if ((size_t)top >= h) top = 0;
    size_t hh = h - (size_t)top;

    size_t bpr = w * 4;
    uint8_t *buf = calloc(bpr * hh, 1);
    if (!buf) return NO;
    CGColorSpaceRef cs = CGColorSpaceCreateDeviceRGB();
    CGContextRef ctx = CGBitmapContextCreate(buf, w, hh, 8, bpr, cs,
                                             kCGImageAlphaPremultipliedLast);
    CGColorSpaceRelease(cs);
    if (!ctx) { free(buf); return NO; }
    // CGBitmapContext origin is bottom-left, so drawing at y=0 keeps the BOTTOM hh rows,
    // i.e. it drops the top `top` rows (Chrome's tab strip + toolbar). Exactly what we want.
    CGContextDrawImage(ctx, CGRectMake(0, 0, (CGFloat)w, (CGFloat)h), img);
    CGContextRelease(ctx);

    long white = 0, bright = 0, n = 0;
    double lumaSum = 0.0;
    long total = (long)(w * hh);
    int step = (int)lround(sqrt((double)total / 40000.0));
    if (step < 1) step = 1;
    for (size_t y = 0; y < hh; y += (size_t)step) {
        uint8_t *row = buf + y * bpr;
        for (size_t x = 0; x < w; x += (size_t)step) {
            uint8_t *p = row + x * 4;
            double r = p[0], g = p[1], b = p[2];
            double luma = 0.2126 * r + 0.7152 * g + 0.0722 * b;
            lumaSum += luma;
            if (r >= 235 && g >= 235 && b >= 235) white++;
            if (luma >= 200) bright++;
            n++;
        }
    }
    free(buf);
    if (n == 0) return NO;
    st->whiteFrac  = (double)white / (double)n;
    st->brightFrac = (double)bright / (double)n;
    st->meanLuma   = lumaSum / (double)n / 255.0;
    st->sampled    = n;
    return YES;
}

#pragma mark - capture

static SCShareableContent *shareableContent(NSError **errOut) {
    __block SCShareableContent *result = nil;
    __block NSError *err = nil;
    dispatch_semaphore_t sem = dispatch_semaphore_create(0);
    [SCShareableContent getShareableContentExcludingDesktopWindows:NO
                                              onScreenWindowsOnly:NO
                                                completionHandler:^(SCShareableContent *c, NSError *e) {
        result = c; err = e;
        dispatch_semaphore_signal(sem);
    }];
    if (dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC)) != 0) {
        if (errOut) *errOut = [NSError errorWithDomain:@"whitecheck" code:1
                                             userInfo:@{NSLocalizedDescriptionKey: @"timed out listing shareable content"}];
        return nil;
    }
    if (errOut) *errOut = err;
    return result;
}

static CGImageRef captureFilter(SCContentFilter *filter, SCStreamConfiguration *cfg, NSError **errOut) {
    __block CGImageRef img = NULL;
    __block NSError *err = nil;
    dispatch_semaphore_t sem = dispatch_semaphore_create(0);
    [SCScreenshotManager captureImageWithFilter:filter
                                 configuration:cfg
                             completionHandler:^(CGImageRef sample, NSError *e) {
        if (sample) img = CGImageRetain(sample);
        err = e;
        dispatch_semaphore_signal(sem);
    }];
    if (dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, 15 * NSEC_PER_SEC)) != 0) {
        if (errOut) *errOut = [NSError errorWithDomain:@"whitecheck" code:2
                                             userInfo:@{NSLocalizedDescriptionKey: @"capture timed out"}];
        return NULL;
    }
    if (errOut) *errOut = err;
    return img;
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        NSMutableArray<NSString *> *args = [NSMutableArray array];
        for (int i = 1; i < argc; i++) [args addObject:@(argv[i])];
        if (args.count == 0)
            die(@"usage: whitecheck --list-chrome | --window <cgid> [--crop-top pt] | --rect x,y,w,h", 2);

        if ([args[0] isEqualToString:@"--list-chrome"]) {
            emit(@{@"windows": listChromeWindows()});
            return 0;
        }

        NSString *mode = nil, *value = nil;
        int cropTopPt = 0;
        for (NSUInteger i = 0; i < args.count; i++) {
            NSString *a = args[i];
            if ([a isEqualToString:@"--window"] || [a isEqualToString:@"--rect"]) {
                if (++i >= args.count) die([@"missing value for " stringByAppendingString:a], 2);
                mode = a; value = args[i];
            } else if ([a isEqualToString:@"--crop-top"]) {
                if (++i >= args.count) die(@"missing value for --crop-top", 2);
                cropTopPt = args[i].intValue;
            } else {
                die([@"unknown argument " stringByAppendingString:a], 2);
            }
        }
        if (!mode) die(@"pick --window or --rect", 2);

        NSError *err = nil;
        SCShareableContent *content = shareableContent(&err);
        if (!content) die(err.localizedDescription ?: @"cannot list shareable content (Screen Recording permission?)", 3);

        SCContentFilter *filter = nil;
        double srcW = 0, srcH = 0;

        if ([mode isEqualToString:@"--window"]) {
            unsigned int wanted = (unsigned int)value.longLongValue;
            SCWindow *found = nil;
            for (SCWindow *w in content.windows) if (w.windowID == wanted) { found = w; break; }
            if (!found) die([NSString stringWithFormat:@"window %u not shareable", wanted], 3);
            filter = [[SCContentFilter alloc] initWithDesktopIndependentWindow:found];
            srcW = filter.contentRect.size.width;
            srcH = filter.contentRect.size.height;
        } else {
            NSArray<NSString *> *p = [value componentsSeparatedByString:@","];
            if (p.count != 4) die(@"--rect needs x,y,w,h", 2);
            CGRect r = CGRectMake(p[0].doubleValue, p[1].doubleValue, p[2].doubleValue, p[3].doubleValue);
            SCDisplay *disp = nil;
            CGPoint c = CGPointMake(CGRectGetMidX(r), CGRectGetMidY(r));
            for (SCDisplay *d in content.displays) if (CGRectContainsPoint(d.frame, c)) { disp = d; break; }
            if (!disp) disp = content.displays.firstObject;
            if (!disp) die(@"no displays", 3);
            filter = [[SCContentFilter alloc] initWithDisplay:disp excludingWindows:@[]];
            srcW = r.size.width; srcH = r.size.height;
        }

        if (srcW < 1) srcW = 1;
        if (srcH < 1) srcH = 1;
        double scale = 480.0 / srcW;
        if (scale > 1.0) scale = 1.0;

        SCStreamConfiguration *cfg = [[SCStreamConfiguration alloc] init];
        cfg.width  = (NSInteger)MAX(64.0, srcW * scale);
        cfg.height = (NSInteger)MAX(64.0, srcH * scale);
        cfg.showsCursor = NO;
        if ([mode isEqualToString:@"--rect"]) {
            NSArray<NSString *> *p = [value componentsSeparatedByString:@","];
            SCDisplay *disp = nil;
            CGRect r = CGRectMake(p[0].doubleValue, p[1].doubleValue, p[2].doubleValue, p[3].doubleValue);
            CGPoint c = CGPointMake(CGRectGetMidX(r), CGRectGetMidY(r));
            for (SCDisplay *d in content.displays) if (CGRectContainsPoint(d.frame, c)) { disp = d; break; }
            if (!disp) disp = content.displays.firstObject;
            cfg.sourceRect = CGRectMake(r.origin.x - disp.frame.origin.x,
                                        r.origin.y - disp.frame.origin.y,
                                        r.size.width, r.size.height);
        }

        CGImageRef img = captureFilter(filter, cfg, &err);
        if (!img) die(err.localizedDescription ?: @"capture failed (Screen Recording permission?)", 3);

        Stats st = {0};
        int cropPx = (int)lround((double)cropTopPt * scale);
        if (!measureImage(img, cropPx, &st)) { CGImageRelease(img); die(@"could not read pixels", 3); }

        emit(@{
            @"white_frac":  @(round(st.whiteFrac  * 1000) / 1000.0),
            @"bright_frac": @(round(st.brightFrac * 1000) / 1000.0),
            @"mean_luma":   @(round(st.meanLuma   * 1000) / 1000.0),
            @"sampled":     @(st.sampled),
            @"px": @{@"w": @((int)CGImageGetWidth(img)), @"h": @((int)CGImageGetHeight(img))},
        });
        CGImageRelease(img);
    }
    return 0;
}
