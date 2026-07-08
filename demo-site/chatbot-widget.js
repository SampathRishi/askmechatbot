/* ============================================================================
 * Cameron County — Embeddable Chatbot Widget
 * Drop-in: add ONE line to any page and the launcher + chat panel appear.
 *     <script src="chatbot-widget.js"></script>
 *
 * Swap in your real chatbot below. `iframeUrl` is the only required change —
 * point it at your hosted chatbot (e.g. the local RAG UI: http://127.0.0.1:8000).
 * ==========================================================================*/
const CHATBOT_CONFIG = {
  // Same-origin root: the chatbot UI is served by the same server that serves
  // this demo page (server.py mounts the demo at /demo and the chat UI at /),
  // so "/" works identically in local dev and in production. Override with a
  // full URL (e.g. "https://your-service.onrender.com") to point at a remote backend.
  iframeUrl: "/",                             // Cameron County RAG assistant (server.py)
  buttonColor: "#033f88",                     // Cameron County blue
  title: "Cameron County Assistant",
  width: 500,                                 // chat panel width (px)
  height: 720,                                // chat panel height (px)
};

(function () {
  "use strict";

  // don't inject twice if the script is included on multiple pages / twice
  if (window.__ccChatWidgetLoaded) return;
  window.__ccChatWidgetLoaded = true;

  var C = CHATBOT_CONFIG;
  var Z = 2147483000; // sit above virtually everything on the page

  // ---- styles (scoped by the cc- prefix so they can't clash with the site) --
  var css = ''
    + '#cc-chat-launcher{position:fixed;right:24px;bottom:24px;width:60px;height:60px;'
    + 'border-radius:50%;border:none;cursor:pointer;background:' + C.buttonColor + ';'
    + 'box-shadow:0 6px 18px rgba(0,0,0,.28);display:flex;align-items:center;'
    + 'justify-content:center;z-index:' + Z + ';transition:transform .15s ease,box-shadow .15s ease;'
    + 'padding:0;}'
    + '#cc-chat-launcher:hover{transform:scale(1.08);box-shadow:0 8px 22px rgba(0,0,0,.34);}'
    + '#cc-chat-launcher svg{width:28px;height:28px;fill:#fff;pointer-events:none;}'
    + '#cc-chat-panel{position:fixed;right:24px;bottom:96px;width:' + C.width + 'px;height:' + C.height + 'px;'
    + 'max-width:calc(100vw - 32px);max-height:calc(100vh - 104px);background:#fff;'
    + 'border-radius:16px;overflow:hidden;box-shadow:0 16px 48px rgba(0,0,0,.32);'
    + 'z-index:' + Z + ';display:none;flex-direction:column;'
    + 'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;}'
    + '#cc-chat-panel.cc-open{display:flex;animation:cc-pop .18s ease;}'
    + '@keyframes cc-pop{from{opacity:0;transform:translateY(12px);}to{opacity:1;transform:translateY(0);}}'
    + '#cc-chat-header{background:' + C.buttonColor + ';color:#fff;display:flex;align-items:center;'
    + 'justify-content:space-between;padding:12px 16px;flex:0 0 auto;}'
    + '#cc-chat-header .cc-title{font-size:15px;font-weight:600;line-height:1.2;}'
    + '#cc-chat-header .cc-controls{display:flex;align-items:center;gap:2px;}'
    + '#cc-chat-close,#cc-chat-expand{background:transparent;border:none;color:#fff;'
    + 'cursor:pointer;width:30px;height:30px;border-radius:6px;padding:0;'
    + 'display:flex;align-items:center;justify-content:center;}'
    + '#cc-chat-close{font-size:22px;line-height:1;}'
    + '#cc-chat-expand svg{width:16px;height:16px;fill:#fff;pointer-events:none;}'
    + '#cc-chat-close:hover,#cc-chat-expand:hover{background:rgba(255,255,255,.18);}'
    + '#cc-chat-panel.cc-full{inset:0;width:100vw;height:100vh;max-width:100vw;'
    + 'max-height:100vh;border-radius:0;}'
    + '#cc-chat-body{flex:1 1 auto;position:relative;background:#f4f6f8;}'
    + '#cc-chat-body iframe{width:100%;height:100%;border:none;display:block;}'
    + '#cc-chat-placeholder{padding:22px;color:#334;font-size:14px;line-height:1.5;'
    + 'height:100%;box-sizing:border-box;display:flex;flex-direction:column;'
    + 'align-items:center;justify-content:center;text-align:center;gap:10px;}'
    + '#cc-chat-placeholder code{background:#e6ebf2;padding:2px 6px;border-radius:4px;'
    + 'font-size:12px;word-break:break-all;}'
    + '@media (max-width:480px){#cc-chat-panel{right:12px;bottom:88px;}'
    + '#cc-chat-launcher{right:16px;bottom:16px;}}';

  var style = document.createElement("style");
  style.id = "cc-chat-styles";
  style.textContent = css;
  document.head.appendChild(style);

  // ---- launcher button -----------------------------------------------------
  var btn = document.createElement("button");
  btn.id = "cc-chat-launcher";
  btn.setAttribute("aria-label", "Open " + C.title);
  btn.setAttribute("type", "button");
  btn.innerHTML =
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 2H4a2 2 0 0 0-2 2v18l4-4h14a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2zM7 9h10v2H7V9zm0 4h7v2H7v-2z"/></svg>';

  // ---- chat panel ----------------------------------------------------------
  var panel = document.createElement("div");
  panel.id = "cc-chat-panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", C.title);

  var isPlaceholder = !C.iframeUrl || /REPLACE_WITH_MY_CHATBOT_URL/i.test(C.iframeUrl);
  var bodyHtml = isPlaceholder
    ? '<div id="cc-chat-placeholder"><strong>' + esc(C.title) + '</strong>'
      + '<div>Set <code>iframeUrl</code> in <code>chatbot-widget.js</code> '
      + 'to your chatbot URL to go live.</div></div>'
    : '<iframe src="' + esc(C.iframeUrl) + '" title="' + esc(C.title) + '" '
      + 'allow="clipboard-write; microphone"></iframe>';

  // header icons: expand-to-fullscreen and restore-to-window
  var EXPAND_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h6V2H2v8h2V4zm16 0v6h2V2h-8v2h6zM4 20v-6H2v8h8v-2H4zm16 0h-6v2h8v-8h-2v6z"/></svg>';
  var RESTORE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 10h6V4H8v4H4v2zm10-6v6h6V8h-4V4h-2zm6 10h-6v6h2v-4h4v-2zM4 14v2h4v4h2v-6H4z"/></svg>';

  panel.innerHTML =
    '<div id="cc-chat-header">'
    + '<span class="cc-title">' + esc(C.title) + '</span>'
    + '<div class="cc-controls">'
    + '<button id="cc-chat-expand" type="button" aria-label="Expand to full screen">' + EXPAND_ICON + '</button>'
    + '<button id="cc-chat-close" type="button" aria-label="Close chat">&times;</button>'
    + '</div>'
    + '</div>'
    + '<div id="cc-chat-body">' + bodyHtml + '</div>';

  // ---- wire up -------------------------------------------------------------
  function mount() {
    document.body.appendChild(btn);
    document.body.appendChild(panel);
    btn.addEventListener("click", toggle);
    panel.querySelector("#cc-chat-close").addEventListener("click", close);
    var expandBtn = panel.querySelector("#cc-chat-expand");
    expandBtn.addEventListener("click", function () {
      var full = panel.classList.toggle("cc-full");
      expandBtn.innerHTML = full ? RESTORE_ICON : EXPAND_ICON;
      expandBtn.setAttribute("aria-label", full ? "Restore chat window" : "Expand to full screen");
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { panel.classList.remove("cc-full"); close(); }
    });
  }

  function toggle() { panel.classList.contains("cc-open") ? close() : open(); }
  function open() {
    panel.classList.add("cc-open");
    btn.setAttribute("aria-expanded", "true");
  }
  function close() {
    panel.classList.remove("cc-open");
    panel.classList.remove("cc-full");
    var eb = panel.querySelector("#cc-chat-expand");
    if (eb) { eb.innerHTML = EXPAND_ICON; eb.setAttribute("aria-label", "Expand to full screen"); }
    btn.setAttribute("aria-expanded", "false");
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
