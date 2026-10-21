(() => {
  "use strict";

  // --- copy-to-clipboard (delegated; works for htmx-swapped content) ---
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy-target]");
    if (!button) return;
    const source = document.getElementById(button.dataset.copyTarget);
    if (!source) return;
    // textContent strips syntax-highlight <span>s and decodes entities;
    // trim() removes the trailing newline templates leave inside <pre>.
    const text = source.textContent.trim();
    try {
      await navigator.clipboard.writeText(text);
    } catch (err) {
      // Fallback for non-secure contexts / older browsers.
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "absolute";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e) { /* give up silently */ }
      document.body.removeChild(ta);
    }
    const previous = button.getAttribute("data-label") || button.textContent;
    button.textContent = "Copied";
    button.classList.add("is-copied");
    window.setTimeout(() => {
      button.textContent = previous;
      button.classList.remove("is-copied");
    }, 1200);
  });

  // --- auto-inject a copy button on every multi-line <pre> code block ---
  // CSP-safe: no inline JS, pure DOM. Skips blocks that already have a copy
  // control, and re-runs after htmx swaps so dynamic content is covered too.
  let autoCopyId = 0;
  // ux3 N3: true when an explicit [data-copy-target] control already copies
  // this <pre> or an element inside it (dashboard snippets target <code id>).
  function hasCopyControl(pre) {
    const ids = [pre.id, ...Array.from(pre.querySelectorAll("[id]"), (el) => el.id)].filter(Boolean);
    return ids.some((id) => document.querySelector(`[data-copy-target="${CSS.escape(id)}"]`));
  }
  function addCopyButtons(root) {
    const scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll("pre").forEach((pre) => {
      if (pre.closest(".code-wrap")) return;           // already wrapped
      if (pre.dataset.noCopy !== undefined) return;    // opt-out hook
      // Skip if a copy button already targets this pre or anything inside it
      // (e.g. normalized JSON, or the dashboard snippets whose explicit Copy
      // targets the inner <code id>): exactly one copy control per block.
      if (hasCopyControl(pre)) return;
      if (!pre.textContent.trim()) return;             // empty
      if (!pre.id) pre.id = `codeblock-${++autoCopyId}`;

      const wrap = document.createElement("div");
      wrap.className = "code-wrap";
      pre.parentNode.insertBefore(wrap, pre);
      wrap.appendChild(pre);

      const button = document.createElement("button");
      button.type = "button";
      button.className = "copy code-copy";
      button.textContent = "Copy";
      button.setAttribute("data-label", "Copy");
      button.setAttribute("data-copy-target", pre.id);
      button.setAttribute("aria-label", "Copy code to clipboard");
      wrap.appendChild(button);
    });
  }
  // --- keyboard access for scrollable code blocks and tables (a11y) ---
  // axe scrollable-region-focusable / WCAG 2.1.1: a horizontally scrollable
  // region must be reachable by keyboard. The templates already render
  // tabindex="0" + role="region" + aria-label on every <pre>, table wrapper and
  // landing code region (works without JS); this is the fallback for any block
  // that reaches the DOM without them (JS-built content, future templates).
  // The visible focus ring comes from the global :focus-visible rule.
  const SCROLL_REGIONS = [
    ["pre", "Code example"],
    [".landing-step-code", "Code example"],
    [".landing-response-head code", "Code example"],
    [".table-wrap", "Table"],
    [".docs-table-wrap", "Table"],
  ];
  function makeScrollRegionsFocusable(root) {
    const scope = root && root.querySelectorAll ? root : document;
    SCROLL_REGIONS.forEach(([selector, label]) => {
      const found = Array.from(scope.querySelectorAll(selector));
      if (scope !== document && scope.matches && scope.matches(selector)) found.push(scope);
      found.forEach((el) => {
        if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "0");
        if (!el.hasAttribute("role")) el.setAttribute("role", "region");
        if (!el.hasAttribute("aria-label") && !el.hasAttribute("aria-labelledby")) el.setAttribute("aria-label", label);
      });
    });
  }

  function enhance(root) {
    addCopyButtons(root);
    makeScrollRegionsFocusable(root);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => enhance(document));
  } else {
    enhance(document);
  }

  // --- htmx error responses (QA-DASH #8) ---
  // htmx 2.x does not swap 4xx/5xx by default, so every dashboard form error
  // (slug taken 409, invalid input 422, rate limit 429, …) was silently
  // dropped. The views render a flash/list partial with the error status.
  // QA-DASH r2: a 4xx is swapped ONLY when the server positively marked it as
  // one of our partials with the `HX-Partial: flash` response header (set
  // centrally by apps/dashboard/partials.py:render_partial). Anything else —
  // a proxy/WAF/Cloudflare page, JSON, plain text, a Django error page, any
  // 5xx — is never injected; a fixed generic flash built from static text
  // (textContent, never the response body) is shown instead.
  // CSP-safe: plain config + listeners in this static file, no inline script.
  const SWAP_ERROR_CODES = "^(400|403|404|409|413|422|429)$";
  const PARTIAL_HEADER = "HX-Partial";
  const PARTIAL_HEADER_VALUE = "flash";
  const GENERIC_ERROR = "Something went wrong. Please try again.";
  if (window.htmx && window.htmx.config) {
    window.htmx.config.responseHandling = [
      { code: "204", swap: false },
      { code: "[23]..", swap: true },
      { code: SWAP_ERROR_CODES, swap: true, error: true },
      { code: "[45]..", swap: false, error: true },
    ];
  }

  // request failed. Only a later successful response from THAT element clears
  // it — the 10 s `recent-messages` poll (or any other unrelated request) must
  // swapped out of the page, the next success from a user-triggered (non-
  // polling) element clears it instead, so the flash can never get stuck.
  let errorFlashOwner = null;

  function requestElt(detail) {
    return (detail && (detail.elt || (detail.requestConfig && detail.requestConfig.elt))) || null;
  }

  function isPolling(elt) {
    const trigger = elt && elt.getAttribute ? elt.getAttribute("hx-trigger") || "" : "";
    return /(^|[\s,])every\s/.test(trigger);
  }

  function showErrorFlash(text, owner) {
    const main = document.getElementById("main-content") || document.body;
    let box = document.getElementById("htmx-error-flash");
    if (!box) {
      box = document.createElement("div");
      box.id = "htmx-error-flash";
      box.className = "flash flash-error";
      box.setAttribute("role", "alert");
      main.insertBefore(box, main.firstChild);
    }
    box.textContent = text;
    errorFlashOwner = owner || null;
  }

  function clearErrorFlashFor(elt) {
    const box = document.getElementById("htmx-error-flash");
    if (!box) return;
    const ownerGone = !errorFlashOwner || errorFlashOwner.isConnected === false;
    if (elt !== errorFlashOwner && !(ownerGone && elt && !isPolling(elt))) return;
    box.remove();
    errorFlashOwner = null;
  }

  document.body.addEventListener("htmx:beforeSwap", (event) => {
    const detail = event.detail;
    const elt = requestElt(detail);
    const status = detail.xhr ? detail.xhr.status : 0;
    if (status < 400) {
      clearErrorFlashFor(elt);
      return;
    }
    // Only a response we provably rendered as a partial may be swapped.
    const marker = detail.xhr && detail.xhr.getResponseHeader
      ? detail.xhr.getResponseHeader(PARTIAL_HEADER)
      : null;
    if (!detail.shouldSwap || marker !== PARTIAL_HEADER_VALUE) {
      detail.shouldSwap = false;
      showErrorFlash(GENERIC_ERROR, elt);
      return;
    }
    // The server's own error partial supersedes this element's generic flash.
    clearErrorFlashFor(elt);
    // Optional per-form error target so a bare flash does not replace a list
    // (e.g. the team members table): data-error-target="#some-id".
    const source = detail.requestConfig && detail.requestConfig.elt;
    const holder = source && source.closest ? source.closest("[data-error-target]") : null;
    const errorTarget = holder ? document.querySelector(holder.getAttribute("data-error-target")) : null;
    if (errorTarget) detail.target = errorTarget;
  });
  document.body.addEventListener("htmx:sendError", (event) => {
    showErrorFlash("Network error. Check your connection and try again.", requestElt(event.detail));
  });

  // --- double-submit guard for plain (non-htmx) mutating forms (QA-DASH #4) ---
  // htmx forms use hx-disabled-elt; full-page POST forms opt in with
  // data-disable-on-submit. The second submit is dropped, and the buttons are
  // disabled on the next tick so the submitter's own name/value (if any) is still sent.
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form || !form.matches || !form.matches("form[data-disable-on-submit]")) return;
    if (form.dataset.submitting === "1") {
      event.preventDefault();
      return;
    }
    form.dataset.submitting = "1";
    window.setTimeout(() => {
      form.querySelectorAll("button, input[type=submit]").forEach((b) => { b.disabled = true; });
    }, 0);
  });
  // Coming back via the back/forward cache must not leave the buttons dead.
  window.addEventListener("pageshow", () => {
    document.querySelectorAll("form[data-disable-on-submit]").forEach((form) => {
      delete form.dataset.submitting;
      form.querySelectorAll("button, input[type=submit]").forEach((b) => { b.disabled = false; });
    });
  });

  // --- csrf + visibility-gated polling (unchanged) ---
  document.body.addEventListener("htmx:configRequest", (event) => {
    const token = document.querySelector("input[name=csrfmiddlewaretoken]");
    if (token) event.detail.headers["X-CSRFToken"] = token.value;
  });
  document.body.addEventListener("htmx:beforeRequest", (event) => {
    const elt = event.detail.elt;
    if (document.hidden && elt && elt.id === "recent-messages") {
      event.preventDefault();
    }
  });
  document.body.addEventListener("htmx:afterSwap", (event) => {
    enhance(event.target);
  });

  // --- qafix2 #15: Esc closes an open "Connect" popover (<details>) ---
  // ux4 r2: and an open inbox "More" menu (details.row-more), the same way.
  // Focus returns to its <summary> so keyboard users stay in place.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const open = document.querySelectorAll("details.connect[open], details.row-more[open]");
    if (!open.length) return;
    open.forEach((details) => {
      details.open = false;
      const summary = details.querySelector("summary");
      if (summary && details.contains(document.activeElement)) summary.focus();
    });
  });

  // --- ux4 r2: inbox "More" menus (details.row-more) ---
  // Delegated, so rows swapped in by htmx are covered. A click outside an open
  // menu closes it, and opening one closes the others (one open at a time).
  // Enter/Space on a <summary> also fire "click", so the keyboard is covered.
  document.addEventListener("click", (event) => {
    const inside = event.target instanceof Element ? event.target.closest("details.row-more") : null;
    document.querySelectorAll("details.row-more[open]").forEach((details) => {
      if (details !== inside) details.open = false;
    });
  });

  // --- qafix2 #17: on the narrow (horizontally scrolling) sidebar nav, bring
  // the active item into view. No-op when the nav does not scroll. ---
  function revealActiveNavItem() {
    const nav = document.querySelector(".sidebar nav");
    if (!nav || nav.scrollWidth <= nav.clientWidth) return;
    const active = nav.querySelector('a[aria-current="page"]');
    if (!active) return;
    const left = active.offsetLeft - nav.offsetLeft;
    const target = left - (nav.clientWidth - active.offsetWidth) / 2;
    nav.scrollLeft = Math.max(0, target);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", revealActiveNavItem);
  } else {
    revealActiveNavItem();
  }

  // --- mobile nav toggle a11y (QA #15), progressive enhancement ---
  // The control is a native checkbox labelled "Menu": Space toggles it,
  // :checked ~ .links shows the menu, and screen readers announce its native
  // checked / not checked state. No role/aria-expanded override — ARIA does
  // not allow role=button on a checkbox (Lighthouse aria-allowed-role).
  // JS only adds keyboard conveniences: Enter toggles, Escape closes.
  const navToggle = document.getElementById("site-nav-toggle");
  if (navToggle) {
    navToggle.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        navToggle.click();
      } else if (event.key === "Escape" && navToggle.checked) {
        navToggle.click();
      }
    });
  }
})();
