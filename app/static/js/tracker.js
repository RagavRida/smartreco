/*
 * SmartReco behavioural tracker.
 *
 * Design constraints (this must never slow the page down):
 *   - Nothing runs on the critical path. Enqueue is a synchronous array push.
 *   - Events are BATCHED and flushed on a timer, on size threshold, on tab hide,
 *     and on unload via navigator.sendBeacon (which survives navigation).
 *   - High-frequency signals (scroll, mousemove-ish) are THROTTLED; search input
 *     is DEBOUNCED so we log intent, not keystrokes.
 *   - Flushes go through requestIdleCallback when available, so we yield to
 *     rendering work first.
 *   - Failures are silent and retried once; tracking must never surface an error
 *     to the user or block navigation.
 */
(function () {
  "use strict";

  var ENDPOINT = "/api/events/batch";
  var FLUSH_INTERVAL_MS = 5000;
  var MAX_BATCH = 25;
  var MAX_QUEUE = 200;
  var SCROLL_THROTTLE_MS = 1000;
  var SEARCH_DEBOUNCE_MS = 700;

  var queue = [];
  var flushTimer = null;
  var sending = false;
  var pageEnteredAt = Date.now();
  var lastActiveAt = Date.now();
  var accumulatedDwellMs = 0;
  var dwellReported = false;

  // ---------------------------------------------------------------- identity
  function uid() {
    return (
      Math.random().toString(36).slice(2, 10) + Math.random().toString(36).slice(2, 10)
    );
  }

  function persistentId(key, storage) {
    try {
      var existing = storage.getItem(key);
      if (existing) return existing;
      var fresh = uid();
      storage.setItem(key, fresh);
      return fresh;
    } catch (e) {
      return uid(); // private mode / storage disabled
    }
  }

  var anonId = persistentId("sr_anon_id", window.localStorage);
  var sessionId = persistentId("sr_session_id", window.sessionStorage);

  // ---------------------------------------------------------------- context
  var page = window.SMARTRECO_PAGE || {};

  function baseEvent(type) {
    return {
      type: type,
      path: window.location.pathname,
      anon_id: anonId,
      session_id: sessionId,
      ts: Date.now()
    };
  }

  // ---------------------------------------------------------------- queueing
  function enqueue(event) {
    if (queue.length >= MAX_QUEUE) queue.shift(); // drop oldest, never grow unbounded
    queue.push(event);
    if (queue.length >= MAX_BATCH) {
      scheduleFlush(0);
    } else if (flushTimer === null) {
      scheduleFlush(FLUSH_INTERVAL_MS);
    }
  }

  function scheduleFlush(delay) {
    if (flushTimer !== null) clearTimeout(flushTimer);
    flushTimer = setTimeout(function () {
      flushTimer = null;
      idle(function () { flush(false); });
    }, delay);
  }

  function idle(fn) {
    if (typeof window.requestIdleCallback === "function") {
      window.requestIdleCallback(fn, { timeout: 2000 });
    } else {
      setTimeout(fn, 0);
    }
  }

  function flush(useBeacon) {
    if (sending || queue.length === 0) return;
    var batch = queue.splice(0, MAX_BATCH);
    var payload = JSON.stringify({ events: batch });

    var signal = document.getElementById("global-tracker-signal");
    if (signal && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      signal.classList.remove("pulse");
      void signal.offsetWidth; // trigger reflow
      signal.classList.add("pulse");
      setTimeout(function() { signal.classList.remove("pulse"); }, 300);
    }

    // Unload path: sendBeacon is fire-and-forget and survives page teardown.
    if (useBeacon && navigator.sendBeacon) {
      try {
        navigator.sendBeacon(ENDPOINT, new Blob([payload], { type: "application/json" }));
        return;
      } catch (e) {
        /* fall through to fetch */
      }
    }

    sending = true;
    fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload,
      keepalive: true,
      credentials: "same-origin"
    })
      .catch(function () {
        // Requeue once so a blip does not silently lose the session's signal.
        if (queue.length + batch.length <= MAX_QUEUE) queue = batch.concat(queue);
      })
      .finally(function () {
        sending = false;
        if (queue.length > 0) scheduleFlush(FLUSH_INTERVAL_MS);
      });
  }

  // ---------------------------------------------------------------- public API
  var SmartReco = {
    track: function (type, data) {
      var event = baseEvent(type);
      for (var key in data || {}) {
        if (Object.prototype.hasOwnProperty.call(data, key)) event[key] = data[key];
      }
      enqueue(event);
    },
    flush: function () { flush(false); },
    queueLength: function () { return queue.length; }
  };
  window.SmartReco = SmartReco;

  // ---------------------------------------------------------------- auto-tracking
  // 1. Page / product view
  if (page.type === "product") {
    SmartReco.track("product_view", {
      product_id: page.product_id,
      slug: page.slug,
      category: page.category,
      meta: { level: page.level, tags: page.tags || [] }
    });
  } else {
    SmartReco.track("page_view", { category: page.category || "" });
  }

  // 2. Search — debounced on input, immediate on submit
  var searchTimer = null;
  document.addEventListener("input", function (e) {
    var input = e.target;
    if (!input || input.getAttribute("data-track") !== "search") return;
    var value = input.value.trim();
    if (value.length < 3) return;
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(function () {
      SmartReco.track("search", { query: value.slice(0, 200) });
    }, SEARCH_DEBOUNCE_MS);
  });

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || form.getAttribute("data-track") !== "search-form") return;
    var input = form.querySelector('[data-track="search"]');
    if (input && input.value.trim()) {
      if (searchTimer) clearTimeout(searchTimer);
      SmartReco.track("search", { query: input.value.trim().slice(0, 200) });
      flush(true); // navigation is imminent
    }
  });

  // 3. Clicks on anything marked up with data-track-click
  document.addEventListener(
    "click",
    function (e) {
      var el = e.target && e.target.closest ? e.target.closest("[data-track-click]") : null;
      if (!el) return;
      SmartReco.track("click", {
        product_id: el.getAttribute("data-product-id") || null,
        slug: el.getAttribute("data-slug") || null,
        category: el.getAttribute("data-category") || "",
        meta: { label: el.getAttribute("data-track-click") }
      });
    },
    true // capture: we log before the navigation handler runs
  );

  // 4. Scroll depth — throttled, one event per 25% milestone
  var reachedDepths = {};
  var lastScrollAt = 0;
  window.addEventListener(
    "scroll",
    function () {
      var now = Date.now();
      lastActiveAt = now;
      if (now - lastScrollAt < SCROLL_THROTTLE_MS) return;
      lastScrollAt = now;
      var doc = document.documentElement;
      var height = Math.max(doc.scrollHeight - window.innerHeight, 1);
      var pct = Math.min(100, Math.round(((window.scrollY || doc.scrollTop) / height) * 100));
      var bucket = Math.floor(pct / 25) * 25;
      if (bucket >= 25 && !reachedDepths[bucket]) {
        reachedDepths[bucket] = true;
        SmartReco.track("scroll_depth", {
          value: bucket,
          product_id: page.product_id || null,
          category: page.category || ""
        });
      }
    },
    { passive: true } // never block scrolling
  );

  // 5. Dwell time — only counts while the tab is actually visible & the user active
  ["mousemove", "keydown", "click", "touchstart"].forEach(function (evt) {
    window.addEventListener(evt, function () { lastActiveAt = Date.now(); }, { passive: true });
  });

  function accumulateDwell() {
    var now = Date.now();
    var elapsed = now - pageEnteredAt;
    // Ignore stretches where the user was idle for more than 60s.
    if (now - lastActiveAt < 60000) accumulatedDwellMs += elapsed;
    pageEnteredAt = now;
  }

  function reportDwell(useBeacon) {
    accumulateDwell();
    if (accumulatedDwellMs < 2000 || dwellReported) return;
    dwellReported = true;
    SmartReco.track("dwell", {
      dwell_ms: accumulatedDwellMs,
      product_id: page.product_id || null,
      slug: page.slug || null,
      category: page.category || "",
      meta: { level: page.level, tags: page.tags || [] }
    });
    flush(useBeacon);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") {
      accumulateDwell();
      flush(true);
    } else {
      pageEnteredAt = Date.now();
      lastActiveAt = Date.now();
    }
  });

  window.addEventListener("pagehide", function () { reportDwell(true); });
  window.addEventListener("beforeunload", function () { reportDwell(true); });

  // Long sessions: checkpoint dwell every 30s so we do not lose it on a hard exit.
  setInterval(function () {
    accumulateDwell();
    if (accumulatedDwellMs > 15000 && !dwellReported) {
      dwellReported = true;
      SmartReco.track("dwell", {
        dwell_ms: accumulatedDwellMs,
        product_id: page.product_id || null,
        category: page.category || ""
      });
    }
  }, 30000);
})();
