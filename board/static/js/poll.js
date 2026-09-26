/* ELF2 轮询工具（无构建链，纯浏览器 JS）
 *
 * 为什么需要它
 * ------------
 * 全站原来一律用 setInterval(fn, ms) 轮询。setInterval **不等上一次返回**，
 * 一旦某个接口变慢，请求就会重叠堆积：
 *
 *     第 1 个请求还在途 -> 3s 后又发第 2 个 -> 各自都被拖慢 -> 又发第 3、4 个 ...
 *
 * 板端是单进程 Flask + GIL，每个在途请求又占一个 Werkzeug 线程，于是线程数、
 * 上下文切换、GIL 争抢一起雪崩。实测把 /api/voice/status 从 40ms 劣化到
 * 10~27 秒，在途请求 40+，线程涨到 300，重启才恢复。
 *
 * ELF2Poll.loop(fn, ms)
 * --------------------
 * 改成「上一次 settle 之后再排下一次」，因此天然保证**同一轮询同时只有一个
 * 请求在途**；页面隐藏（document.hidden）时跳过，回到前台立刻补一次。
 *
 *     ELF2Poll.loop(loadStatus, 3000);                      // 3s 一轮，不重叠
 *     ELF2Poll.loop(loadStatus, 3000, { immediate: true }); // 先立刻跑一次
 *     ELF2Poll.loop(heavy, 30000, { skipHidden: false });   // 后台也继续
 *
 * 选项：
 *   immediate   首轮是否立即执行（默认 false，即等一个周期）
 *   skipHidden  页面隐藏时是否跳过（默认 true）
 *   maxMs       单轮看门狗时长（默认 max(周期 x 10, 30s)）。
 *               app.js 的 apiFetch 没有超时，万一某个请求一直挂着，
 *               到点强制放行下一轮，避免整条轮询线永久停摆。
 *               代价是最坏情况下容忍 2 个并发（自愈，而非无限堆积）。
 *
 * 返回值：{ stop(), runNow(), isRunning() }
 */
(function () {
  'use strict';

  if (window.ELF2Poll) return;

  function loop(fn, ms, opts) {
    opts = opts || {};
    var skipHidden = opts.skipHidden !== false;
    var interval = Math.max(0, Number(ms) || 0);
    var maxMs = Math.max(0, Number(opts.maxMs) || Math.max(interval * 10, 30000));
    var running = false;
    var stopped = false;
    var timer = null;
    var guard = null;

    function schedule(delay) {
      if (stopped) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(run, Math.max(0, delay));
    }

    function release() {
      running = false;
      if (guard) { clearTimeout(guard); guard = null; }
    }

    function run() {
      timer = null;
      if (stopped) return;
      // 在途保护：上一轮还没结束就顺延，绝不并发叠加
      if (running) { schedule(interval); return; }
      if (skipHidden && document.hidden) { schedule(interval); return; }

      running = true;
      var settled = false;
      guard = setTimeout(function () {
        guard = null;
        if (settled) return;
        settled = true;
        release();
        if (window.console && console.warn) {
          console.warn('[poll] 单轮超过 ' + maxMs + 'ms 未返回，已放行下一轮');
        }
        schedule(interval);
      }, maxMs);

      Promise.resolve()
        .then(fn)
        .catch(function (e) {
          if (window.console && console.warn) console.warn('[poll] 轮询出错：', e);
        })
        .then(function () {
          if (settled) return;
          settled = true;
          release();
          schedule(interval);
        });
    }

    function stop() {
      stopped = true;
      if (timer) { clearTimeout(timer); timer = null; }
      if (guard) { clearTimeout(guard); guard = null; }
      running = false;
    }

    function runNow() {
      if (stopped) return;
      if (timer) { clearTimeout(timer); timer = null; }
      run();
    }

    if (skipHidden) {
      // 从后台切回前台时立刻补一次，避免隐藏期间完全停摆后还要等一整个周期
      document.addEventListener('visibilitychange', function () {
        if (!document.hidden) runNow();
      });
    }

    schedule(opts.immediate ? 0 : interval);
    return { stop: stop, runNow: runNow, isRunning: function () { return running; } };
  }

  window.ELF2Poll = { loop: loop };
})();
