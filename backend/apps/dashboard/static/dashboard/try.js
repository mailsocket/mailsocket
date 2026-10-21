(() => {
  "use strict";

  const button = document.getElementById("try-new-inbox");
  const error = document.getElementById("try-error");
  const inboxCard = document.getElementById("try-inbox");
  const addressEl = document.getElementById("try-address");
  const statusEl = document.getElementById("try-status");
  const otpEl = document.getElementById("try-otp");
  const fromEl = document.getElementById("try-from");
  const contextEl = document.getElementById("try-context");
  const providerEl = document.getElementById("try-provider");
  const providerLabel = document.getElementById("try-provider-label");

  function setProvider(value) {
    providerEl.textContent = value || "";
    providerEl.hidden = !value;
    if (providerLabel) providerLabel.hidden = !value;
  }
  const sampleButton = document.getElementById("try-send-sample");

  let inboxId = null;
  let since = "0";
  let pollToken = 0;
  let inFlight = false;
  // r2 poll-retry state (see poll() below).
  const BACKOFF_START_MS = 1200;
  const BACKOFF_MAX_MS = 30000;
  const RETRY_AFTER_DEFAULT_S = 2;
  let backoffMs = BACKOFF_START_MS;
  let retryTimer = null;
  let stopped = false;

  function showError(message) {
    error.textContent = message;
    error.hidden = false;
  }

  function clearError() {
    error.textContent = "";
    error.hidden = true;
  }

  function setStatus(text, waiting) {
    statusEl.textContent = text;
    if (waiting) {
      statusEl.setAttribute("data-waiting", text);
    } else {
      statusEl.removeAttribute("data-waiting");
    }
  }

  function csrfToken() {
    const input = document.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  function createInbox() {
    clearError();
    button.disabled = true;
    fetch(button.dataset.endpoint, {
      method: "POST",
      headers: {
        "X-CSRFToken": csrfToken(),
        "X-Requested-With": "XMLHttpRequest",
        Accept: "application/json",
      },
    })
      .then((response) => {
        if (response.status === 429) {
          showError("You've created several demo inboxes already. Please wait a little before trying again.");
          button.disabled = false;
          return null;
        }
        if (!response.ok) {
          showError("Couldn't create a demo inbox. Please try again.");
          button.disabled = false;
          return null;
        }
        return response.json();
      })
      .then((data) => {
        button.disabled = false;
        if (!data || !data.id) return;
        inboxId = data.id;
        since = "0";
        // r2: a fresh inbox resets the poll state — clears a prior 404 stop
        // and error backoff, and orphans any in-flight wait for the old inbox
        // (its response fails the pollToken check and is ignored).
        stopped = false;
        backoffMs = BACKOFF_START_MS;
        clearRetryTimer();
        pollToken++;
        inFlight = false;
        addressEl.textContent = data.address;
        inboxCard.hidden = false;
        otpEl.textContent = "—";
        fromEl.textContent = "—";
        contextEl.textContent = "—";
        setProvider(null);
        if (sampleButton) sampleButton.disabled = false;
        setStatus("Calling wait_for_otp (long-poll, timeout=25s)…", true);
        poll();
      })
      .catch(() => {
        button.disabled = false;
        showError("Couldn't reach the demo service. Please try again.");
      });
  }

  function sendSample() {
    if (!inboxId || !sampleButton) return;
    clearError();
    sampleButton.disabled = true;
    const url = sampleButton.dataset.sampleBase.replace("__ID__", inboxId);
    fetch(url, {
      method: "POST",
      headers: {
        "X-CSRFToken": csrfToken(),
        "X-Requested-With": "XMLHttpRequest",
        Accept: "application/json",
      },
    })
      .then((response) => {
        sampleButton.disabled = false;
        if (response.status === 429) {
          showError("Too many sample codes requested. Please wait a little before trying again.");
          return;
        }
        if (!response.ok) {
          showError("Couldn't send a sample code. Please try again.");
        }
        // On success the already-running long-poll call above picks up the
        // new message on its next response — no separate fetch needed here.
      })
      .catch(() => {
        sampleButton.disabled = false;
        showError("Couldn't reach the demo service. Please try again.");
      });
  }

  // One long-poll call per "cycle": the server blocks up to timeout=25s.
  // 204 (nothing yet). Every other outcome is scheduled, never immediate:
  //   429          -> wait Retry-After seconds (default 2) + 0-1s jitter
  //   404          -> stop for good; the demo inbox expired / was purged
  //   5xx/network  -> exponential backoff 1.2s, x2, capped at 30s; reset
  //                   after any successful 200/204
  // While the tab is hidden nothing polls; visibilitychange resumes it.

  function clearRetryTimer() {
    if (retryTimer !== null) {
      window.clearTimeout(retryTimer);
      retryTimer = null;
    }
  }

  function scheduleRetry(delayMs) {
    clearRetryTimer();
    retryTimer = window.setTimeout(() => {
      retryTimer = null;
      poll();
    }, delayMs);
  }

  function retryAfterMs(response) {
    const header = response.headers.get("Retry-After");
    const seconds = parseInt(header, 10);
    const base = Number.isFinite(seconds) && seconds > 0 ? seconds : RETRY_AFTER_DEFAULT_S;
    return base * 1000 + Math.floor(Math.random() * 1000);
  }

  function backoffDelay() {
    const delay = backoffMs;
    backoffMs = Math.min(backoffMs * 2, BACKOFF_MAX_MS);
    return delay;
  }

  function poll() {
    if (!inboxId || inFlight || stopped || document.hidden) return;
    clearRetryTimer();
    const token = ++pollToken;
    inFlight = true;
    const url = button.dataset.waitBase.replace("__ID__", inboxId) +
      "?timeout=25&since=" + encodeURIComponent(since);
    fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
    })
      .then((response) => {
        if (token !== pollToken) return null;
        if (response.status === 204) {
          return { kind: "empty" };
        }
        if (response.status === 429) {
          return { kind: "throttled", delay: retryAfterMs(response) };
        }
        if (response.status === 404) {
          return { kind: "gone" };
        }
        if (!response.ok) {
          return { kind: "error" };
        }
        return response.json().then((data) => ({ kind: "data", data: data }));
      })
      .then((outcome) => {
        if (token !== pollToken || !outcome) return;
        inFlight = false;
        if (outcome.kind === "throttled") {
          setStatus("Too many waits right now — retrying shortly.", true);
          scheduleRetry(outcome.delay);
          return;
        }
        if (outcome.kind === "gone") {
          stopped = true;
          if (sampleButton) sampleButton.disabled = true;
          setStatus("This demo inbox expired, create a new one", false);
          return;
        }
        if (outcome.kind === "error") {
          setStatus("The demo service hiccuped — retrying…", true);
          scheduleRetry(backoffDelay());
          return;
        }
        // 200 or 204: success — reset the error backoff.
        backoffMs = BACKOFF_START_MS;
        const message = outcome.kind === "data" && outcome.data ? outcome.data.data : null;
        if (message) {
          otpEl.textContent = message.otp || "—";
          fromEl.textContent = message.from || "—";
          contextEl.textContent = message.otp_context || "—";
          setProvider(message.provider_hint);
          setStatus("Code received. Send another code to see it update.", false);
          // Advance the cursor past the message we just showed so the next
          // poll only matches strictly newer arrivals.
          if (message.id) since = message.id;
        } else {
          setStatus("Calling wait_for_otp (long-poll, timeout=25s)…", true);
        }
        // Immediate re-issue: this IS the long-poll, not an interval timer.
        poll();
      })
      .catch(() => {
        if (token !== pollToken) return;
        inFlight = false;
        // Network failure: exponential backoff, never a tight loop.
        setStatus("Couldn't reach the demo service — retrying…", true);
        scheduleRetry(backoffDelay());
      });
  }

  // Hidden tab: stop polling (pending retry timers cancelled; an in-flight
  // request finishes but is not re-issued). Visible again: resume at once.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearRetryTimer();
    } else {
      poll();
    }
  });

  // The wait endpoint base URL is baked into the page as a data attribute so
  // this file stays static and CSP-safe (no inline config, no inline script).
  button.addEventListener("click", createInbox);
  if (sampleButton) sampleButton.addEventListener("click", sendSample);

  // Copy-to-clipboard (the address button + auto-injected <pre> buttons) is
  // provided by app.js, which this page now loads (gate R2: shared nav a11y).
  // Keeping a second delegated copy handler here would double-fire and leave
  // the address button stuck on "Copied".
})();
