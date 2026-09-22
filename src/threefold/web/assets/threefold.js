/*
 * Threefold, the layer every page shares.
 *
 * One file, no library, loaded by every page before its own script: where the
 * API is, who the reader is signed in as, a fetch that turns a refusal into a
 * signed-out state a reader can act on, the navigation, and the few inline SVG
 * charts the dashboard draws. Nothing here holds a number of its own: every
 * figure a page shows is handed to these helpers from an API response.
 *
 * Everything rendered from the service goes through escapeHtml, directly or
 * through the html`` template below, which escapes every value it is given
 * unless that value is itself html`` output. A project name, a target or a
 * reason is never written into a page as markup.
 */
(function (root) {
  'use strict';

  // The server rewrites the token below to the API Gateway stage prefix when it
  // serves this file, exactly as it does for the pages. Opened from disk the
  // token stays in place, and the pages fall back to a local server.
  var SERVED_BASE_PATH = "__THREEFOLD_BASE_PATH__";
  var LOCAL = SERVED_BASE_PATH.indexOf("THREEFOLD_BASE_PATH") !== -1;
  var ORIGIN = (root.location && root.location.origin) || '';
  var API_BASE = LOCAL ? 'http://localhost:8001' : ORIGIN + SERVED_BASE_PATH;
  // Sibling pages sit under the same stage prefix, which a bare relative href
  // loses when a page is reached without its trailing slash.
  var PAGE_BASE = LOCAL ? '' : ORIGIN + SERVED_BASE_PATH + '/';
  // The address commands are written for. It always ends in a slash: API
  // Gateway answers the bare stage path with its own 404.
  var STACK_URL = LOCAL ? 'http://localhost:8001/' : ORIGIN + SERVED_BASE_PATH + '/';

  // The stack's default AllowedProjectPattern. The service stays the judge; a
  // name outside it is stored as "unlabelled", so a page never offers one.
  var PROJECT_PATTERN = /^Acme-[A-Za-z0-9-]{1,40}$/;

  var SESSION_STORAGE = 'threefold-session';
  var OPERATOR_KEY_STORAGE = 'threefold-operator-key';

  // ------------------------------------------------------------- escaping

  function escapeHtml(str) {
    // Everything is made a string first and then escaped. Returning a non-string
    // as String(value) unescaped let a tool_name sent as an array reach the page
    // as markup.
    if (typeof str !== 'string') str = str == null ? '' : String(str);
    return str.replace(/[&<>"']/g, function (m) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[m];
    });
  }

  // Markup this layer built itself. Only html`` and raw() make one, and only
  // setHtml() writes one into the page, so an untrusted string cannot reach
  // innerHTML without being escaped on the way.
  function SafeHtml(markup) { this.__html = markup; }
  SafeHtml.prototype.toString = function () { return this.__html; };

  function toMarkup(value) {
    if (value instanceof SafeHtml) return value.__html;
    if (Array.isArray(value)) return value.map(toMarkup).join('');
    if (value === false || value === null || value === undefined) return '';
    return escapeHtml(value);
  }

  function html(strings) {
    var out = strings[0];
    for (var i = 1; i < strings.length; i++) out += toMarkup(arguments[i]) + strings[i];
    return new SafeHtml(out);
  }

  // For markup this file or a page wrote as a literal, never for a value read
  // from the service.
  function raw(markup) { return new SafeHtml(String(markup)); }

  function setHtml(el, safe) {
    if (!el) return;
    if (!(safe instanceof SafeHtml)) throw new TypeError('setHtml takes html`` output, never a plain string');
    el.innerHTML = safe.__html;
  }

  // --------------------------------------------------------------- storage

  // A sign-in is kept in this browser as {token, expires_at}. Storage can be
  // blocked or throw, so every access is guarded, and a sign-in made while it is
  // blocked lives in memory for as long as the tab does.
  var memorySession = null;

  function expiryMs(value) {
    if (value === null || value === undefined || value === '') return NaN;
    var n = typeof value === 'number' ? value : (/^\d+(\.\d+)?$/.test(String(value)) ? Number(value) : NaN);
    if (!isNaN(n)) return n < 1e12 ? n * 1000 : n;
    return Date.parse(String(value));
  }

  function readSession() {
    var saved = null;
    try {
      var text = root.localStorage.getItem(SESSION_STORAGE);
      if (text) saved = JSON.parse(text);
    } catch (err) { saved = null; }
    if (!saved) saved = memorySession;
    if (!saved || typeof saved.token !== 'string' || !saved.token) return null;
    var ends = expiryMs(saved.expires_at);
    if (!isNaN(ends) && ends <= Date.now()) {
      clearSession();
      return null;
    }
    return { token: saved.token, expires_at: saved.expires_at, endsAt: isNaN(ends) ? null : ends };
  }

  // True when the sign-in was written to storage, false when it lives in memory.
  function writeSession(token, expiresAt) {
    memorySession = { token: String(token), expires_at: expiresAt };
    try {
      root.localStorage.setItem(SESSION_STORAGE, JSON.stringify(memorySession));
      return true;
    } catch (err) {
      return false;
    }
  }

  function clearSession() {
    memorySession = null;
    try { root.localStorage.removeItem(SESSION_STORAGE); } catch (err) { /* nothing stored */ }
  }

  // The key pasted on the settings page, the fallback from before sign-in.
  function storedOperatorKey() {
    try { return (root.localStorage.getItem(OPERATOR_KEY_STORAGE) || '').trim(); } catch (err) { return ''; }
  }

  // After a sign-in the browser holds a session, never the operator key.
  function forgetOperatorKey() {
    try {
      if (!root.localStorage.getItem(OPERATOR_KEY_STORAGE)) return false;
      root.localStorage.removeItem(OPERATOR_KEY_STORAGE);
      return true;
    } catch (err) {
      return false;
    }
  }

  // A key typed into a page's own field is the reader's choice for that request
  // and goes first. Otherwise a live sign-in goes as a bearer token, and only
  // without one the key stored on the settings page, as X-API-Key.
  function authHeaders(extra, typedKey) {
    var headers = { Accept: 'application/json' };
    var name;
    for (name in (extra || {})) if (Object.prototype.hasOwnProperty.call(extra, name)) headers[name] = extra[name];
    var typed = (typedKey || '').trim();
    if (typed) {
      headers['X-API-Key'] = typed;
      return headers;
    }
    var session = readSession();
    if (session) {
      headers.Authorization = 'Bearer ' + session.token;
      return headers;
    }
    var key = storedOperatorKey();
    if (key) headers['X-API-Key'] = key;
    return headers;
  }

  function credentialInUse() {
    if (readSession()) return 'session';
    if (storedOperatorKey()) return 'key';
    return null;
  }

  // ----------------------------------------------------------------- fetch

  var authListeners = [];
  function onAuthChange(fn) { authListeners.push(fn); }
  // reason: 'signin', 'signout', or 'expired' when the stack stopped accepting
  // the sign-in this browser held.
  function announceAuth(reason) {
    whoamiPromise = null;
    authListeners.slice().forEach(function (fn) { try { fn(reason); } catch (err) { /* a listener's own problem */ } });
  }

  function ApiError(status, body, path, kind, presented) {
    this.status = status;
    this.body = body || null;
    this.path = path;
    this.kind = kind;
    // What this browser sent, so a refusal can say which credential failed.
    this.presented = presented || null;
    var detail = body && (body.detail || body.title || body.message);
    this.message = kind === 'network'
      ? 'The service could not be reached.'
      : 'HTTP ' + status + (detail ? ': ' + detail : '');
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  function isAuthError(err) { return !!err && err.kind === 'auth'; }

  // One request to this stack's API. Resolves with the parsed body; rejects with
  // an ApiError whose kind is 'auth' (401 or 403), 'not_found', 'http' or
  // 'network'. A sign-in the service no longer accepts is dropped here, once,
  // so every page falls back to the signed-out state together.
  function api(path, options) {
    options = options || {};
    var method = options.method || 'GET';
    var extra = options.body !== undefined ? { 'Content-Type': 'application/json' } : null;
    var headers = authHeaders(extra, options.typedKey);
    var presented = headers.Authorization ? 'session' : (headers['X-API-Key'] ? 'key' : null);
    var init = { method: method, headers: headers };
    if (options.body !== undefined) init.body = JSON.stringify(options.body);
    return root.fetch(API_BASE + path, init).then(function (res) {
      var parse = typeof res.json === 'function' ? res.json() : Promise.resolve(null);
      return Promise.resolve(parse).catch(function () { return null; }).then(function (body) {
        if (res.ok) return body;
        var kind = (res.status === 401 || res.status === 403) ? 'auth' : res.status === 404 ? 'not_found' : 'http';
        if (res.status === 401 && presented === 'session') {
          clearSession();
          announceAuth('expired');
        }
        throw new ApiError(res.status, body, path, kind, presented);
      });
    }, function () {
      throw new ApiError(0, null, path, 'network', presented);
    });
  }

  var whoamiPromise = null;
  // What the stack says about this reader and itself, or null on a stack that
  // does not answer the route. Asked once per page and again after a sign-in or
  // a sign-out.
  function whoami(force) {
    if (force) whoamiPromise = null;
    if (!whoamiPromise) {
      whoamiPromise = api('/api/auth/whoami').then(function (body) {
        return body && typeof body === 'object' ? body : null;
      }, function () { return null; });
    }
    return whoamiPromise;
  }

  function signOut() {
    var had = readSession();
    var done = had
      ? api('/api/auth/sessions', { method: 'DELETE' }).catch(function () { return null; })
      : Promise.resolve(null);
    return done.then(function () {
      clearSession();
      announceAuth('signout');
    });
  }

  // The state a page shows when the stack refused to show it. It says what to
  // run, not only what went wrong, and names the fallback.
  function signedOutHtml(err, what) {
    var presented = err && err.presented;
    var status = err && err.status;
    var headline;
    var cause;
    if (presented === 'session') {
      headline = 'Your sign-in has ended';
      cause = 'This browser was signed in, and the stack no longer accepts that sign-in: it expired, or it was signed out.';
    } else if (presented === 'key') {
      headline = 'The operator key stored in this browser was not accepted';
      cause = 'The stack answered HTTP ' + status + ' to the key pasted on the settings page.';
    } else if (status === 403) {
      headline = 'This needs the operator';
      cause = 'The stack answered HTTP 403: reading this may be open, but this action is the operator\'s.';
    } else {
      headline = 'Sign in to read this stack';
      cause = 'This stack keeps its ledger private, so ' + (what || 'this page') + ' needs a sign-in.';
    }
    return html`<div class="tf-card border-amber-900/60 p-5 space-y-3" role="alert" data-state="signed-out">
      <div class="flex items-start gap-3">
        <span class="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-amber-950 text-amber-300 border border-amber-800" aria-hidden="true">${raw(ICONS.lock)}</span>
        <div class="space-y-1 min-w-0">
          <h2 class="text-sm font-semibold text-white">${headline}</h2>
          <p class="text-xs text-gray-400">${cause}</p>
        </div>
      </div>
      <div class="space-y-1.5">
        <p class="text-xs text-gray-300">On the machine that holds the operator key, in the folder where you ran connect:</p>
        ${commandBlock('python threefold.py open', 'Sign-in command')}
        <p class="text-[11px] text-gray-500">It opens this dashboard already signed in. The link it uses works once and only for a short while, and the browser keeps a sign-in, never the key.</p>
      </div>
      <p class="text-[11px] text-gray-500 border-t border-gray-800 pt-3">No <span class="font-mono">threefold.py</span> yet? <a class="tf-link" href="${pageHref('dashboard.html#/connect')}">Connect a repository</a> first. As a fallback, the operator key can still be <a class="tf-link" href="${pageHref('settings.html#operator-key')}">pasted on the settings page</a>, where it is kept in this browser.</p>
    </div>`;
  }

  // A plain error with the next thing to do. `retry` names a data-action a page
  // handles; the network case says the likely cause.
  function errorHtml(err, what, retryAction) {
    var network = err && err.kind === 'network';
    return html`<div class="tf-card border-rose-900/60 p-5 space-y-2" role="alert" data-state="error">
      <h2 class="text-sm font-semibold text-rose-300">${network ? 'The service could not be reached' : 'The service refused ' + (what || 'the request')}</h2>
      <p class="text-xs text-gray-400">${network
        ? 'Nothing is shown rather than something invented. Check the connection, or that the stack at ' + API_BASE + ' is up.'
        : (err && err.message ? err.message : 'Unknown error') + '. Nothing is shown rather than something invented.'}</p>
      ${retryAction ? html`<button type="button" class="tf-btn" data-action="${retryAction}">Try again</button>` : ''}
    </div>`;
  }

  // ----------------------------------------------------------- formatting

  function isNumber(value) { return typeof value === 'number' && isFinite(value); }

  // A count as a reader reads it, or a dash for a value the service did not give.
  function num(value) {
    if (!isNumber(value)) return '—';
    if (Math.abs(value) >= 100000) return (value / 1000).toFixed(0) + 'K';
    if (Math.abs(value) >= 10000) return (value / 1000).toFixed(1).replace(/\.0$/, '') + 'K';
    return Math.round(value).toLocaleString('en-US');
  }

  function pct(value) {
    if (!isNumber(value)) return '—';
    var p = value * 100;
    return (p > 0 && p < 1 ? '<1' : String(Math.round(p))) + '%';
  }

  function usd(value) {
    if (!isNumber(value)) return '—';
    return '$' + value.toFixed(value < 1 ? 4 : 2);
  }

  function timeAgo(iso) {
    var then = Date.parse(iso);
    if (isNaN(then)) return iso ? String(iso) : '—';
    var seconds = Math.round((Date.now() - then) / 1000);
    if (seconds < 45) return 'just now';
    var minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes + ' min ago';
    var hours = Math.round(minutes / 60);
    if (hours < 24) return hours + ' h ago';
    var days = Math.round(hours / 24);
    return days + ' d ago';
  }

  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  // "2026-09-21" as "21 Sep", read as a calendar day, never shifted by timezone.
  function shortDay(day) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(day || ''));
    if (!m) return String(day || '');
    return Number(m[3]) + ' ' + MONTHS[Number(m[2]) - 1];
  }

  function dateTime(iso) {
    var then = new Date(iso);
    if (isNaN(then.getTime())) return iso ? String(iso) : '—';
    return then.getDate() + ' ' + MONTHS[then.getMonth()] + ' ' + then.getFullYear() + ', ' +
      String(then.getHours()).padStart(2, '0') + ':' + String(then.getMinutes()).padStart(2, '0');
  }

  function clockTime(ms) {
    var then = new Date(ms);
    if (isNaN(then.getTime())) return '';
    return String(then.getHours()).padStart(2, '0') + ':' + String(then.getMinutes()).padStart(2, '0');
  }

  // A query string from an object, leaving out what is empty.
  function query(params) {
    var parts = [];
    Object.keys(params || {}).forEach(function (name) {
      var value = params[name];
      if (value === undefined || value === null || value === '') return;
      parts.push(encodeURIComponent(name) + '=' + encodeURIComponent(value));
    });
    return parts.length ? '?' + parts.join('&') : '';
  }

  function pageHref(path) { return PAGE_BASE + path; }

  // Swagger UI derives an operation's anchor from its method and path when the
  // document declares no operationIds; the pages link to those anchors.
  function specHref(method, path) {
    return PAGE_BASE + 'swagger.html#/default/' + method.toLowerCase() + path.replace(/[^A-Za-z0-9]/g, '_');
  }

  // A new session id per call, as the other pages make them: a fixed one would
  // be shared by every visitor.
  function newSessionId(prefix) {
    var c = root.crypto;
    var hex = '';
    if (c && typeof c.randomUUID === 'function') return (prefix || 'sim-') + c.randomUUID();
    if (c && typeof c.getRandomValues === 'function') {
      c.getRandomValues(new Uint8Array(16)).forEach(function (b) { hex += b.toString(16).padStart(2, '0'); });
    } else {
      for (var i = 0; i < 32; i++) hex += Math.floor(Math.random() * 16).toString(16);
    }
    return (prefix || 'sim-') + hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' + hex.slice(16, 20) + '-' + hex.slice(20, 32);
  }

  // ------------------------------------------------------------ clipboard

  function copyText(text, button) {
    var done = function (ok) {
      if (!button) return;
      var original = button.getAttribute('data-label') || button.textContent;
      if (!button.getAttribute('data-label') && button.setAttribute) button.setAttribute('data-label', original);
      button.textContent = ok ? 'Copied' : 'Select and copy';
      setTimeout(function () { button.textContent = original; }, 1600);
    };
    var nav = root.navigator;
    if (nav && nav.clipboard && typeof nav.clipboard.writeText === 'function') {
      nav.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
    } else {
      done(false);
    }
  }

  // A command a reader copies. The text is what they run, so it is shown whole,
  // selectable, with a copy button beside it.
  function commandBlock(command, label) {
    return html`<div class="tf-command group">
      <pre class="tf-pre" aria-label="${label || 'Command'}"><code>${command}</code></pre>
      <button type="button" class="tf-copy" data-tf-copy="${command}" aria-label="Copy ${label || 'the command'}">Copy</button>
    </div>`;
  }

  // ----------------------------------------------------------------- icons

  var ICONS = {
    lock: '<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="4" y="9" width="12" height="8" rx="2"/><path d="M7 9V6.5a3 3 0 0 1 6 0V9"/></svg>',
    eye: '<svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 10s3-5.5 8-5.5S18 10 18 10s-3 5.5-8 5.5S2 10 2 10z"/><circle cx="10" cy="10" r="2.5"/></svg>',
    shield: '<svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M10 2.5 16 5v4.5c0 4-2.6 6.8-6 8-3.4-1.2-6-4-6-8V5l6-2.5z"/><path d="m7.5 10 1.8 1.8L13 8"/></svg>',
    arrow: '<svg viewBox="0 0 20 20" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><path d="M7 5l5 5-5 5"/></svg>',
    menu: '<svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 6h14M3 10h14M3 14h14"/></svg>',
    refresh: '<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M16 10a6 6 0 1 1-1.8-4.3M16 3.5v3.2h-3.2"/></svg>'
  };

  // ------------------------------------------------------------ navigation

  var NAV = [
    { id: 'overview', label: 'Overview', page: 'dashboard.html', hash: '#/overview' },
    { id: 'review', label: 'Review', page: 'dashboard.html', hash: '#/review' },
    { id: 'projects', label: 'Projects', page: 'dashboard.html', hash: '#/projects' },
    { id: 'rules', label: 'Rules', page: 'rules.html' },
    { id: 'sessions', label: 'Sessions', page: 'sessions.html' },
    { id: 'connect', label: 'Connect', page: 'dashboard.html', hash: '#/connect' },
    { id: 'settings', label: 'Settings', page: 'settings.html' },
    { id: 'demo', label: 'Demo', page: 'index.html' }
  ];

  function navHref(item, inDashboard) {
    if (item.hash && inDashboard) return item.hash;
    return PAGE_BASE + item.page + (item.hash || '');
  }

  var mounted = [];

  // Draws the shared navigation into `el`: the links, what kind of stack this
  // is, and whether this browser is signed in. On a narrow screen the links fold
  // into a menu. `active` is the id of the page or route the reader is on.
  function mountNav(el, options) {
    if (!el) return;
    options = options || {};
    var entry = null;
    for (var i = 0; i < mounted.length; i++) if (mounted[i].el === el) entry = mounted[i];
    if (!entry) {
      entry = { el: el, options: options };
      mounted.push(entry);
    }
    entry.options = options;
    drawNav(entry, null);
    whoami().then(function (who) { drawNav(entry, who); });
  }

  function drawNav(entry, who) {
    var options = entry.options;
    var links = NAV.map(function (item) {
      var current = item.id === options.active;
      return { item: item, current: current, href: navHref(item, options.inDashboard) };
    });
    setHtml(entry.el, html`<div class="flex items-center gap-2 justify-end w-full">
      <nav aria-label="Threefold" class="hidden lg:flex items-center gap-0.5">
        ${links.map(function (l) {
          return html`<a href="${l.href}" class="tf-nav-link${l.current ? ' tf-nav-current' : ''}"${l.current ? raw(' aria-current="page"') : ''}>${l.item.label}</a>`;
        })}
        <a href="${PAGE_BASE}swagger.html" class="tf-nav-link text-gray-500" title="The published OpenAPI document">API</a>
      </nav>
      <div class="flex items-center gap-1.5">${authChips(who)}</div>
      <button type="button" class="lg:hidden tf-icon-btn" data-tf-menu aria-expanded="false" aria-controls="tf-menu" aria-label="Open the menu">${raw(ICONS.menu)}</button>
      <div id="tf-menu" class="hidden lg:hidden absolute left-0 right-0 top-full border-b border-gray-800 bg-gray-950/95 backdrop-blur px-4 py-3">
        <nav aria-label="Threefold, menu" class="grid grid-cols-2 gap-1">
          ${links.map(function (l) {
            return html`<a href="${l.href}" class="tf-nav-link${l.current ? ' tf-nav-current' : ''}"${l.current ? raw(' aria-current="page"') : ''}>${l.item.label}</a>`;
          })}
          <a href="${PAGE_BASE}swagger.html" class="tf-nav-link text-gray-500">API</a>
        </nav>
      </div>
    </div>`);
  }

  // The stack kind comes from the stack; the sign-in from the stack when it
  // answers whoami, and from this browser alone when it does not.
  function authChips(who) {
    var chips = [];
    if (who && who.reads_public === true) {
      chips.push(html`<span class="tf-chip tf-chip-sky" title="Anyone can read this stack's ledger: it is the public demo">Public demo</span>`);
    } else if (who && who.reads_public === false) {
      chips.push(html`<span class="tf-chip tf-chip-gray" title="This stack's ledger is read only by its operator">Private stack</span>`);
    }
    var local = readSession();
    var via = who ? (who.authenticated ? who.via : null) : credentialInUse();
    if (who && !who.authenticated && local) {
      // The stack does not know the sign-in this browser holds.
      clearSession();
      local = null;
    }
    if (via === 'session') {
      var ends = expiryMs(who && who.expires_at ? who.expires_at : (local && local.expires_at));
      chips.push(html`<span class="tf-chip tf-chip-emerald" title="Signed in to this stack from the command line">Signed in${isNaN(ends) ? '' : ' · until ' + clockTime(ends)}</span>`);
      chips.push(html`<button type="button" class="tf-chip tf-chip-button" data-tf-signout>Sign out</button>`);
    } else if (via === 'key') {
      chips.push(html`<a href="${PAGE_BASE}settings.html#operator-key" class="tf-chip tf-chip-amber" title="Using the operator key pasted on the settings page; signing in with python threefold.py open replaces it">Operator key</a>`);
    } else if (!who || who.reads_public === false) {
      chips.push(html`<a href="${PAGE_BASE}dashboard.html#/signin" class="tf-chip tf-chip-amber" title="How to sign in">Signed out</a>`);
    } else {
      chips.push(html`<a href="${PAGE_BASE}dashboard.html#/signin" class="tf-chip tf-chip-gray" title="The operator signs in with python threefold.py open">Sign in</a>`);
    }
    return chips;
  }

  function redrawNavs() {
    mounted.forEach(function (entry) {
      drawNav(entry, null);
      whoami().then(function (who) { drawNav(entry, who); });
    });
  }
  onAuthChange(redrawNavs);

  function closestAttr(target, name) {
    var node = target;
    while (node && node !== root.document) {
      if (typeof node.hasAttribute === 'function' && node.hasAttribute(name)) return node;
      node = node.parentNode;
    }
    return null;
  }

  function toggleMenu(button, open) {
    var menu = root.document.getElementById('tf-menu');
    if (!menu || !menu.classList) return;
    var show = open === undefined ? menu.classList.contains('hidden') : open;
    menu.classList.toggle('hidden', !show);
    if (button && button.setAttribute) button.setAttribute('aria-expanded', show ? 'true' : 'false');
  }

  if (root.document && typeof root.document.addEventListener === 'function') {
    root.document.addEventListener('click', function (event) {
      var target = event && event.target;
      if (!target) return;
      var copy = closestAttr(target, 'data-tf-copy');
      if (copy) { copyText(copy.getAttribute('data-tf-copy'), copy); return; }
      var menu = closestAttr(target, 'data-tf-menu');
      if (menu) { toggleMenu(menu); return; }
      if (closestAttr(target, 'data-tf-signout')) { signOut(); return; }
      // Following a link in the folded menu closes it.
      if (target.closest && target.closest('#tf-menu a')) toggleMenu(null, false);
    });
    root.document.addEventListener('keydown', function (event) {
      if (event && event.key === 'Escape') toggleMenu(null, false);
    });
  }

  // ---------------------------------------------------------------- charts
  //
  // Inline SVG, drawn from the numbers a page is handed. Every chart carries a
  // <title> and <desc>, a table a screen reader can read in its place, a legend
  // where there is more than one series, and a link on every mark to the rows
  // behind it. The outcome colours were checked for colour-blind separation on
  // the dark card; they sit in the band that needs a second channel, which is
  // why the legend and the 2px gaps between segments are never left out.

  var COLORS = {
    approved: '#059669',
    observed: '#d97706',
    refused: '#e11d48',
    accent: '#8b5cf6',
    track: '#1f2937',
    grid: '#1f2937',
    axis: '#6b7280',
    surface: '#111827'
  };

  var chartSeq = 0;
  function chartId(prefix) { chartSeq += 1; return (prefix || 'tf-chart') + '-' + chartSeq; }

  function niceStep(max, ticks) {
    if (!(max > 0)) return 1;
    var rough = max / (ticks || 3);
    var power = Math.pow(10, Math.floor(Math.log(rough) / Math.LN10));
    var steps = [1, 2, 5, 10];
    for (var i = 0; i < steps.length; i++) if (steps[i] * power >= rough) return Math.max(1, steps[i] * power);
    return Math.max(1, 10 * power);
  }

  // A bar with a rounded data end and a square foot, as a path.
  function roundedTop(x, y, w, h, r) {
    r = Math.max(0, Math.min(r, w / 2, h));
    return 'M' + x + ',' + (y + h) + 'V' + (y + r) + 'Q' + x + ',' + y + ' ' + (x + r) + ',' + y +
      'H' + (x + w - r) + 'Q' + (x + w) + ',' + y + ' ' + (x + w) + ',' + (y + r) + 'V' + (y + h) + 'Z';
  }

  function round1(v) { return Math.round(v * 10) / 10; }

  // Days as stacked columns. `series` is [{day, <key>: count, ...}], `keys` is
  // the stack from the baseline up: [{key, label, color}]. `href(row, key)` is
  // where a segment leads.
  function stackedBars(series, options) {
    options = options || {};
    var keys = options.keys || [];
    var width = Math.max(260, Math.round(options.width || 640));
    var height = options.height || 200;
    var pad = { top: 10, right: 8, bottom: 24, left: 36 };
    var plotW = width - pad.left - pad.right;
    var plotH = height - pad.top - pad.bottom;
    var rows = series || [];
    var totals = rows.map(function (row) {
      return keys.reduce(function (sum, k) { return sum + (isNumber(row[k.key]) ? row[k.key] : 0); }, 0);
    });
    var max = Math.max.apply(null, [0].concat(totals));
    var step = niceStep(max, 3);
    var top = Math.max(step, Math.ceil(max / step) * step);
    var slot = rows.length ? plotW / rows.length : plotW;
    var barW = Math.max(3, Math.min(24, slot * 0.66));
    var id = chartId('tf-stack');
    var y = function (v) { return pad.top + plotH - (v / top) * plotH; };

    var grid = [];
    for (var t = 0; t <= top; t += step) {
      grid.push(html`<line x1="${pad.left}" x2="${width - pad.right}" y1="${round1(y(t))}" y2="${round1(y(t))}" stroke="${COLORS.grid}" stroke-width="1"/>
        <text x="${pad.left - 6}" y="${round1(y(t) + 3.5)}" text-anchor="end" class="tf-axis">${num(t)}</text>`);
    }

    var every = Math.max(1, Math.ceil(rows.length / Math.max(1, Math.floor(plotW / 46))));
    var bars = rows.map(function (row, i) {
      var x = round1(pad.left + i * slot + (slot - barW) / 2);
      var base = pad.top + plotH;
      var drawn = [];
      var present = keys.filter(function (k) { return isNumber(row[k.key]) && row[k.key] > 0; });
      present.forEach(function (k, j) {
        var value = row[k.key];
        var h = (value / top) * plotH;
        var isTop = j === present.length - 1;
        // The 2px gap in the surface colour separates one segment from the next.
        var gap = j > 0 ? 2 : 0;
        var drawnH = Math.max(1, h - gap);
        var yTop = base - h;
        var shape = isTop
          ? html`<path class="tf-mark" d="${roundedTop(x, round1(yTop), round1(barW), round1(drawnH), 4)}" fill="${k.color}"/>`
          : html`<rect class="tf-mark" x="${x}" y="${round1(yTop)}" width="${round1(barW)}" height="${round1(drawnH)}" fill="${k.color}"/>`;
        var label = shortDay(row.day) + ': ' + num(value) + ' ' + k.label.toLowerCase();
        var href = options.href ? options.href(row, k.key) : null;
        drawn.push(href
          ? html`<a href="${href}" aria-label="${label}, open these calls"><title>${label} — open these calls</title>${shape}</a>`
          : html`<g><title>${label}</title>${shape}</g>`);
        base -= h;
      });
      // Counted back from the newest day, so today always carries its label.
      var tick = (rows.length - 1 - i) % every === 0
        ? html`<text x="${round1(pad.left + i * slot + slot / 2)}" y="${height - 6}" text-anchor="middle" class="tf-axis">${shortDay(row.day)}</text>`
        : '';
      return html`<g>${drawn}${tick}</g>`;
    });

    var table = html`<table class="sr-only"><caption>${options.title || 'Calls per day'}</caption>
      <thead><tr><th scope="col">Day</th>${keys.map(function (k) { return html`<th scope="col">${k.label}</th>`; })}</tr></thead>
      <tbody>${rows.map(function (row) {
        return html`<tr><th scope="row">${shortDay(row.day)}</th>${keys.map(function (k) { return html`<td>${num(row[k.key])}</td>`; })}</tr>`;
      })}</tbody></table>`;

    return html`<figure class="tf-figure">
      <svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-labelledby="${id}-t ${id}-d" class="tf-svg">
        <title id="${id}-t">${options.title || 'Calls per day'}</title>
        <desc id="${id}-d">${options.desc || ''}</desc>
        ${grid}${bars}
      </svg>
      ${legend(keys)}
      ${table}
    </figure>`;
  }

  function legend(keys) {
    if (!keys || keys.length < 2) return '';
    return html`<ul class="tf-legend" aria-hidden="true">${keys.map(function (k) {
      return html`<li><span class="tf-swatch" style="background:${k.color}"></span>${k.label}</li>`;
    })}</ul>`;
  }

  // One row per thing, each a bar of one or more segments on a shared scale.
  // rows: [{label, href, sublabel, segments: [{value, label, color, href}]}]
  function hbars(rows, options) {
    options = options || {};
    var totals = rows.map(function (r) {
      return r.segments.reduce(function (s, seg) { return s + (isNumber(seg.value) ? seg.value : 0); }, 0);
    });
    var max = options.max || Math.max.apply(null, [1].concat(totals));
    var keys = options.keys || [];
    return html`<figure class="tf-figure">
      <ul class="space-y-3" aria-label="${options.title || ''}">
        ${rows.map(function (r, i) {
          var x = 0;
          var segs = r.segments.filter(function (seg) { return isNumber(seg.value) && seg.value > 0; });
          var described = r.segments.map(function (seg) { return num(seg.value) + ' ' + seg.label; }).join(', ');
          return html`<li>
            <div class="flex items-baseline justify-between gap-3 text-xs">
              ${r.href ? html`<a href="${r.href}" class="tf-link-quiet truncate font-mono">${r.label}</a>` : html`<span class="truncate font-mono text-gray-200">${r.label}</span>`}
              <span class="shrink-0 font-mono tabular-nums text-gray-300">${r.value !== undefined ? r.value : num(totals[i])}</span>
            </div>
            <svg viewBox="0 0 100 8" preserveAspectRatio="none" width="100%" height="8" class="mt-1.5 block tf-svg" role="img" aria-label="${r.label}: ${described}">
              <rect x="0" y="0" width="100" height="8" rx="0" fill="${COLORS.track}"/>
              ${segs.map(function (seg, j) {
                var w = (seg.value / max) * 100;
                var gap = j > 0 ? 0.6 : 0;
                var rect = html`<rect class="tf-mark" x="${round1(x + gap)}" y="0" width="${Math.max(0.6, round1(w - gap))}" height="8" fill="${seg.color}"/>`;
                x += w;
                var label = r.label + ': ' + num(seg.value) + ' ' + seg.label;
                return seg.href
                  ? html`<a href="${seg.href}" aria-label="${label}, open these calls"><title>${label} — open these calls</title>${rect}</a>`
                  : html`<g><title>${label}</title>${rect}</g>`;
              })}
            </svg>
            ${r.sublabel ? html`<p class="mt-1 text-[11px] text-gray-500">${r.sublabel}</p>` : ''}
          </li>`;
        })}
      </ul>
      ${legend(keys)}
    </figure>`;
  }

  // Parts of a whole as a ring. parts: [{label, value, color, href}]
  function donut(parts, options) {
    options = options || {};
    var size = options.size || 132;
    var thick = options.thickness || 14;
    var r = (size - thick) / 2;
    var c = 2 * Math.PI * r;
    var total = parts.reduce(function (s, p) { return s + (isNumber(p.value) ? p.value : 0); }, 0);
    var id = chartId('tf-donut');
    var offset = 0;
    var shown = parts.filter(function (p) { return isNumber(p.value) && p.value > 0; });
    var arcs = shown.map(function (p) {
      var len = total ? (p.value / total) * c : 0;
      var dash = shown.length > 1 ? Math.max(0.5, len - 2) : len;
      var arc = html`<circle class="tf-mark" cx="${size / 2}" cy="${size / 2}" r="${round1(r)}" fill="none" stroke="${p.color}" stroke-width="${thick}" stroke-dasharray="${round1(dash)} ${round1(c - dash)}" stroke-dashoffset="${round1(-offset)}" transform="rotate(-90 ${size / 2} ${size / 2})"/>`;
      offset += len;
      var label = p.label + ': ' + num(p.value) + ' (' + pct(total ? p.value / total : 0) + ')';
      return p.href
        ? html`<a href="${p.href}" aria-label="${label}, open these calls"><title>${label} — open these calls</title>${arc}</a>`
        : html`<g><title>${label}</title>${arc}</g>`;
    });
    return html`<figure class="tf-figure flex items-center gap-4">
      <svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img" aria-labelledby="${id}-t" class="shrink-0 tf-svg">
        <title id="${id}-t">${options.title || 'Share'}: ${parts.map(function (p) { return p.label + ' ' + num(p.value); }).join(', ')}</title>
        <circle cx="${size / 2}" cy="${size / 2}" r="${round1(r)}" fill="none" stroke="${COLORS.track}" stroke-width="${thick}"/>
        ${arcs}
        <text x="${size / 2}" y="${size / 2 + 2}" text-anchor="middle" class="tf-donut-value">${options.center !== undefined ? options.center : num(total)}</text>
        <text x="${size / 2}" y="${size / 2 + 18}" text-anchor="middle" class="tf-axis">${options.sub || ''}</text>
      </svg>
      <ul class="space-y-1.5 text-xs min-w-0">${parts.map(function (p) {
        var inner = html`<span class="tf-swatch" style="background:${p.color}"></span><span class="text-gray-300">${p.label}</span><span class="ml-auto pl-3 font-mono tabular-nums text-gray-400">${num(p.value)}</span>`;
        return html`<li>${p.href ? html`<a href="${p.href}" class="flex items-center gap-2 tf-link-row">${inner}</a>` : html`<span class="flex items-center gap-2">${inner}</span>`}</li>`;
      })}</ul>
    </figure>`;
  }

  // A trend line for a tile. values: numbers, oldest first.
  function sparkline(values, options) {
    options = options || {};
    var width = options.width || 96;
    var height = options.height || 26;
    var vals = (values || []).map(function (v) { return isNumber(v) ? v : 0; });
    if (vals.length < 2) return '';
    var max = Math.max.apply(null, [1].concat(vals));
    var step = (width - 6) / (vals.length - 1);
    var points = vals.map(function (v, i) {
      return round1(3 + i * step) + ',' + round1(height - 3 - (v / max) * (height - 6));
    });
    var last = points[points.length - 1].split(',');
    var color = options.color || COLORS.accent;
    return html`<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="${options.label || 'Trend'}: ${vals.join(', ')}" class="tf-svg shrink-0">
      <polyline points="${points.join(' ')} ${round1(3 + (vals.length - 1) * step)},${height - 3} 3,${height - 3}" fill="${color}" fill-opacity="0.1" stroke="none"/>
      <polyline points="${points.join(' ')}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="${last[0]}" cy="${last[1]}" r="3" fill="${color}" stroke="${COLORS.surface}" stroke-width="2"/>
    </svg>`;
  }

  root.Threefold = {
    SERVED_BASE_PATH: SERVED_BASE_PATH,
    LOCAL: LOCAL,
    API_BASE: API_BASE,
    PAGE_BASE: PAGE_BASE,
    STACK_URL: STACK_URL,
    PROJECT_PATTERN: PROJECT_PATTERN,
    SESSION_STORAGE: SESSION_STORAGE,
    OPERATOR_KEY_STORAGE: OPERATOR_KEY_STORAGE,
    COLORS: COLORS,
    ICONS: ICONS,
    escapeHtml: escapeHtml,
    html: html,
    raw: raw,
    setHtml: setHtml,
    SafeHtml: SafeHtml,
    readSession: readSession,
    writeSession: writeSession,
    clearSession: clearSession,
    storedOperatorKey: storedOperatorKey,
    forgetOperatorKey: forgetOperatorKey,
    authHeaders: authHeaders,
    credentialInUse: credentialInUse,
    api: api,
    ApiError: ApiError,
    isAuthError: isAuthError,
    whoami: whoami,
    signOut: signOut,
    onAuthChange: onAuthChange,
    announceAuth: announceAuth,
    signedOutHtml: signedOutHtml,
    errorHtml: errorHtml,
    num: num,
    pct: pct,
    usd: usd,
    timeAgo: timeAgo,
    shortDay: shortDay,
    dateTime: dateTime,
    clockTime: clockTime,
    query: query,
    pageHref: pageHref,
    specHref: specHref,
    newSessionId: newSessionId,
    copyText: copyText,
    commandBlock: commandBlock,
    mountNav: mountNav,
    stackedBars: stackedBars,
    hbars: hbars,
    donut: donut,
    sparkline: sparkline,
    legend: legend
  };
})(typeof window !== 'undefined' ? window : this);
