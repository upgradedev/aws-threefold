/*
 * Threefold, the layer every page shares.
 *
 * One file, no library, loaded by every page before its own script: where the
 * API is, who the reader is signed in as, a fetch that turns a refusal into a
 * signed-out state a reader can act on, the shell (the header's navigation,
 * the menu sheet on a phone, the command palette), the design system's
 * components and icons, the motion helpers, and the inline SVG charts the
 * dashboard draws. Nothing here holds a number of its own: every figure a page
 * shows is handed to these helpers from an API response.
 *
 * Everything rendered from the service goes through escapeHtml, directly or
 * through the html`` template below, which escapes every value it is given
 * unless that value is itself html`` output. A project name, a target or a
 * reason is never written into a page as markup. Text a tooltip or a live
 * region shows is written with textContent, never as markup.
 *
 * Every part of this file runs where the browser objects it reaches for are
 * missing (the page tests run it under Node with a stub document), so each
 * reach for createElement, matchMedia, requestAnimationFrame or querySelector
 * is guarded, and a missing one means "do nothing", never an error.
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
  // The reader's own names for the aliases the API knows. Kept in this browser
  // and nowhere else: see the local names section below.
  var LOCAL_NAMES_STORAGE = 'threefold-local-names';
  var LOCAL_NAMES_HIDDEN_STORAGE = 'threefold-local-names-hidden';
  var LOCAL_NAME_MAX = 60;
  var LOCAL_NAMES_MAX = 200;

  var doc = root.document || null;

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

  // ----------------------------------------------------------- local names
  //
  // The ledger knows a project only by its alias, because no real repository
  // name may leave the owner's machine. The owner, though, cannot tell one
  // alias from another. So a reader may keep their own name for each alias in
  // this browser, and every page shows it beside the alias, quieter.
  //
  // Three rules hold this together, and the rest of the file depends on them:
  //   1. A label is display text and nothing else. It is never put in a URL, a
  //      query string, a request body, a header, a copied command or the value
  //      of a field a handler reads back. Only projectName() and
  //      projectNameText() render one, and neither is a data source.
  //   2. It is escaped like every other untrusted string: projectName() goes
  //      through html``, projectNameText() returns a plain string that its
  //      caller interpolates through html`` in turn.
  //   3. It lives in localStorage alone. Nothing here ever calls the API.
  //
  // The command palette may match what the reader types against a label, so a
  // reader can find a project by their own name for it. The match happens in
  // this browser; the palette's only request is GET /api/projects, with no
  // query, and a project it opens is reached by its alias alone.

  var memoryNames = null;
  var memoryHidden = null;
  // A browser can let a read through and refuse a write: an old private mode,
  // or a full quota. Storage then still answers with whatever it held before,
  // which is not what this tab was told, so once a write has been refused this
  // tab answers from memory until one succeeds. Without this the panel's
  // promise — kept for this tab, at least — was false on that browser: the
  // name went nowhere and every page drew the bare alias again.
  var namesMemoryOnly = false;
  var hiddenMemoryOnly = false;
  // JSON.parse only when the stored text changed, so a table of a hundred rows
  // parses the map once rather than once a row. `undefined` means "not read
  // yet", which `getItem` never returns, so clearing the names does not leave
  // the cache looking current when storage says null.
  var namesText;
  var namesCache = {};

  // A map of alias to label, with anything that is not one dropped: a label is
  // a single line of at most LOCAL_NAME_MAX characters, and at most
  // LOCAL_NAMES_MAX of them are kept. `__proto__` is skipped rather than
  // assigned, so a pasted document cannot reach an object's prototype.
  function cleanNames(value) {
    var clean = {};
    if (!value || typeof value !== 'object' || Array.isArray(value)) return clean;
    var aliases = Object.keys(value);
    var kept = 0;
    for (var i = 0; i < aliases.length && kept < LOCAL_NAMES_MAX; i++) {
      var alias = aliases[i];
      if (!alias || alias === '__proto__') continue;
      var label = value[alias];
      if (typeof label !== 'string') continue;
      label = label.replace(/\s+/g, ' ').trim().slice(0, LOCAL_NAME_MAX);
      if (!label) continue;
      clean[alias] = label;
      kept += 1;
    }
    return clean;
  }

  // What this browser has stored, as a clean map. Storage can be blocked or
  // throw, so the fallback is whatever this tab set while it was.
  function readLocalNames() {
    if (namesMemoryOnly) return memoryNames || {};
    var text = null;
    try {
      text = root.localStorage.getItem(LOCAL_NAMES_STORAGE);
    } catch (err) {
      return memoryNames || {};
    }
    if (text === namesText) return namesCache;
    var parsed = null;
    if (text) {
      try { parsed = JSON.parse(text); } catch (err) { parsed = null; }
    }
    namesText = text;
    namesCache = cleanNames(parsed);
    return namesCache;
  }

  // True when the map was written to storage, false when it lives in this tab
  // only, so a panel can say which happened rather than appear to have saved.
  function writeLocalNames(map) {
    var clean = cleanNames(map);
    memoryNames = clean;
    var stored = true;
    try {
      if (Object.keys(clean).length) root.localStorage.setItem(LOCAL_NAMES_STORAGE, JSON.stringify(clean));
      else root.localStorage.removeItem(LOCAL_NAMES_STORAGE);
    } catch (err) {
      stored = false;
    }
    // A refused write leaves storage holding the map from before, so reading it
    // again would hand back names this tab no longer has and drop the ones it
    // was just given. This tab reads from memory until a write goes through.
    namesMemoryOnly = !stored;
    namesText = undefined;
    announceLocalNames();
    return stored;
  }

  // Clear all takes the switch with the names: a name saved after this would
  // otherwise be invisible, with the switch gone from the navigation — it is
  // drawn only once a name is set — and nothing on screen to explain it.
  function clearLocalNames() {
    var namesGone = writeLocalNames({});
    var switchGone = setLocalNamesHidden(false);
    return namesGone && switchGone;
  }

  function localNamesHidden() {
    if (hiddenMemoryOnly) return memoryHidden === true;
    try {
      var text = root.localStorage.getItem(LOCAL_NAMES_HIDDEN_STORAGE);
      if (text === null || text === undefined) return memoryHidden === true;
      return text === '1';
    } catch (err) {
      return memoryHidden === true;
    }
  }

  function setLocalNamesHidden(hidden) {
    memoryHidden = !!hidden;
    var stored = true;
    try {
      if (hidden) root.localStorage.setItem(LOCAL_NAMES_HIDDEN_STORAGE, '1');
      else root.localStorage.removeItem(LOCAL_NAMES_HIDDEN_STORAGE);
    } catch (err) {
      stored = false;
    }
    hiddenMemoryOnly = !stored;
    announceLocalNames();
    return stored;
  }

  var nameListeners = [];
  // Returns the way to stop listening, so a screen that subscribes on every
  // visit does not leave one behind each time.
  function onLocalNamesChange(fn) {
    nameListeners.push(fn);
    return function () { nameListeners = nameListeners.filter(function (other) { return other !== fn; }); };
  }
  function announceLocalNames() {
    nameListeners.slice().forEach(function (fn) { try { fn(); } catch (err) { /* a listener's own problem */ } });
  }

  // The reader's name for this alias, or '' when there is none and whenever the
  // switch in the navigation is off. Every render site goes through here, so
  // one switch turns the lot off for a screenshot.
  function projectLabel(alias) {
    if (typeof alias !== 'string' || !alias) return '';
    if (localNamesHidden()) return '';
    var names = readLocalNames();
    return Object.prototype.hasOwnProperty.call(names, alias) ? names[alias] : '';
  }

  // The alias as the API knows it, and the reader's own name beside it. The
  // alias is always there: it is what every link, filter and request is built
  // from, and the label is never any of those.
  function projectName(alias) {
    var label = projectLabel(alias);
    if (!label) return html`${alias}`;
    return html`${alias}<span class="tf-local-name"> · ${label}</span>`;
  }

  // The same for a title or an aria-label, where markup cannot go. The caller
  // interpolates it through html``, which escapes it.
  function projectNameText(alias) {
    var label = projectLabel(alias);
    return label ? String(alias) + ' · ' + label : String(alias == null ? '' : alias);
  }

  // The switch. It is a button, so it is reachable and operable from the
  // keyboard, and it says what it does rather than only showing a state. It is
  // drawn only once this browser holds a label: with none, every page reads
  // exactly as it did before any of this existed.
  function localNamesToggle() {
    if (!Object.keys(readLocalNames()).length) return '';
    var hidden = localNamesHidden();
    var reads = 'Your names: ' + (hidden ? 'off' : 'on');
    // The words on the button start the name a screen reader and a voice
    // control announce, so someone who says what they can see reaches it
    // (WCAG 2.5.3, Label in Name). What pressing it does follows.
    var says = reads + '. ' + (hidden
      ? 'Your own names for projects are hidden. Press to show them beside each alias.'
      : 'Your own names for projects are shown beside each alias. Press to hide them, for a screenshot or a demo.');
    return html`<button type="button" class="tf-chip tf-chip-button" data-tf-names
      aria-pressed="${hidden ? 'false' : 'true'}" aria-label="${says}" title="${says}">${icon(hidden ? 'eyeoff' : 'eye', 13)}<span>${reads}</span></button>`;
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
    return html`<div class="tf-card tf-state tf-state-warn" role="alert" data-state="signed-out">
      <div class="tf-state-head">
        <span class="tf-state-icon" aria-hidden="true">${icon('lock', 18)}</span>
        <div class="tf-state-copy">
          <h2 class="tf-state-title">${headline}</h2>
          <p class="tf-state-text">${cause}</p>
        </div>
      </div>
      <div class="tf-state-body">
        <p class="tf-small">On the machine that holds the operator key, in the folder where you ran connect:</p>
        ${commandBlock('python threefold.py open', 'Sign-in command')}
        <p class="tf-micro">It opens this dashboard already signed in. The link it uses works once and only for a short while, and the browser keeps a sign-in, never the key.</p>
      </div>
      <p class="tf-state-foot">No <span class="tf-mono">threefold.py</span> yet? <a class="tf-link" href="${pageHref('dashboard.html#/connect')}">Connect a repository</a> first. As a fallback, the operator key can still be <a class="tf-link" href="${pageHref('settings.html#operator-key')}">pasted on the settings page</a>, where it is kept in this browser.</p>
    </div>`;
  }

  // A plain error with the next thing to do. `retry` names a data-action a page
  // handles; the network case says the likely cause.
  function errorHtml(err, what, retryAction) {
    var network = err && err.kind === 'network';
    return html`<div class="tf-card tf-state tf-state-danger" role="alert" data-state="error">
      <div class="tf-state-head">
        <span class="tf-state-icon" aria-hidden="true">${icon('alert', 18)}</span>
        <div class="tf-state-copy">
          <h2 class="tf-state-title">${network ? 'The service could not be reached' : 'The service refused ' + (what || 'the request')}</h2>
          <p class="tf-state-text">${network
            ? 'Nothing is shown rather than something invented. Check the connection, or that the stack at ' + API_BASE + ' is up.'
            : (err && err.message ? err.message : 'Unknown error') + '. Nothing is shown rather than something invented.'}</p>
        </div>
      </div>
      ${retryAction ? html`<div><button type="button" class="tf-btn tf-btn-sm" data-action="${retryAction}">${icon('refresh', 14)}Try again</button></div>` : ''}
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

  // ----------------------------------------------------------------- DOM

  // A node this layer owns, found by id or made at the end of <body>. Under the
  // page tests' stub, getElementById makes one on first use.
  function ownNode(id, className, attrs) {
    if (!doc || typeof doc.getElementById !== 'function') return null;
    var el = doc.getElementById(id);
    if (!el && typeof doc.createElement === 'function' && doc.body && typeof doc.body.appendChild === 'function') {
      el = doc.createElement('div');
      el.id = id;
      if (className) el.className = className;
      Object.keys(attrs || {}).forEach(function (name) { el.setAttribute(name, attrs[name]); });
      doc.body.appendChild(el);
    }
    return el;
  }

  function closestAttr(target, name) {
    var node = target;
    while (node && node !== doc) {
      if (typeof node.hasAttribute === 'function' && node.hasAttribute(name)) return node;
      node = node.parentNode;
    }
    return null;
  }

  function within(node, container) {
    while (node) { if (node === container) return true; node = node.parentNode; }
    return false;
  }

  function all(scope, selector) {
    if (!scope || typeof scope.querySelectorAll !== 'function') return [];
    return Array.prototype.slice.call(scope.querySelectorAll(selector));
  }

  function one(scope, selector) {
    if (!scope || typeof scope.querySelector !== 'function') return null;
    return scope.querySelector(selector);
  }

  function focusNode(node) {
    if (node && typeof node.focus === 'function') { try { node.focus({ preventScroll: false }); } catch (err) { /* focus is a courtesy */ } }
  }

  // One polite live region for what a screen reader should hear once: a copy,
  // a toast, a count of results.
  function announce(text) {
    var region = ownNode('tf-live', 'sr-only', { 'aria-live': 'polite', role: 'status' });
    if (!region) return;
    region.textContent = '';
    setTimeout(function () { region.textContent = String(text || ''); }, 30);
  }

  // Rewrites every <a data-tf-page="rules.html"> to the page under this stack's
  // prefix, which a bare relative href loses without a trailing slash.
  function linkPages(scope) {
    all(scope || doc, 'a[data-tf-page]').forEach(function (a) { a.href = PAGE_BASE + a.getAttribute('data-tf-page'); });
  }

  function isMac() {
    var nav = root.navigator || {};
    return /Mac|iPhone|iPad|iPod/.test(String(nav.platform || '') + ' ' + String(nav.userAgent || ''));
  }

  // ----------------------------------------------------------------- icons
  //
  // One set, stroke 1.5 on a 20px grid, round caps and joins, drawn in the
  // current colour. icon(name, size, className) is the only way to draw one;
  // an unknown name draws nothing.

  var ICON_PATHS = {
    shield: '<path d="M10 2.25 16.25 4.9v4.35c0 4.05-2.65 6.95-6.25 8.5-3.6-1.55-6.25-4.45-6.25-8.5V4.9L10 2.25Z"/><path d="m7.4 10.1 1.85 1.85 3.4-3.7"/>',
    check: '<path d="m4.5 10.5 3.6 3.5 7.4-8"/>',
    x: '<path d="m5.5 5.5 9 9M14.5 5.5l-9 9"/>',
    alert: '<path d="M8.7 3.6 2.6 14.2a1.5 1.5 0 0 0 1.3 2.3h12.2a1.5 1.5 0 0 0 1.3-2.3L11.3 3.6a1.5 1.5 0 0 0-2.6 0Z"/><path d="M10 8v3.25M10 13.9v.1"/>',
    info: '<circle cx="10" cy="10" r="7.25"/><path d="M10 9.25v4.5M10 6.4v.1"/>',
    eye: '<path d="M1.9 10S4.9 4.5 10 4.5 18.1 10 18.1 10 15.1 15.5 10 15.5 1.9 10 1.9 10Z"/><circle cx="10" cy="10" r="2.5"/>',
    eyeoff: '<path d="M3 3l14 14"/><path d="M8.2 4.7A8.6 8.6 0 0 1 10 4.5c5.1 0 8.1 5.5 8.1 5.5a14 14 0 0 1-2.3 3M12.9 14.9a7.4 7.4 0 0 1-2.9.6C4.9 15.5 1.9 10 1.9 10a14.2 14.2 0 0 1 3.4-3.9"/><path d="M8.3 8.3a2.5 2.5 0 0 0 3.4 3.4"/>',
    lock: '<rect x="4" y="8.75" width="12" height="8.5" rx="2"/><path d="M6.75 8.75V6.5a3.25 3.25 0 0 1 6.5 0v2.25"/>',
    git: '<circle cx="6" cy="4.5" r="1.75"/><circle cx="6" cy="15.5" r="1.75"/><circle cx="14" cy="7" r="1.75"/><path d="M6 6.25v7.5M14 8.75c0 3.25-3.75 3.25-7.4 5.2"/>',
    terminal: '<rect x="2.75" y="3.75" width="14.5" height="12.5" rx="2"/><path d="m6 8 2.5 2L6 12M10.5 12.5h3.5"/>',
    sparkles: '<path d="M9 2.75 10.5 7l4.25 1.5L10.5 10 9 14.25 7.5 10 3.25 8.5 7.5 7 9 2.75Z"/><path d="m15.25 12.5.65 1.6 1.6.65-1.6.65-.65 1.6-.65-1.6-1.6-.65 1.6-.65.65-1.6Z"/>',
    chart: '<path d="M3.75 16.25h12.5M6.25 13.25v-4M10 13.25v-7.5M13.75 13.25v-5.5"/>',
    list: '<path d="M7.5 5.5h9M7.5 10h9M7.5 14.5h9M3.75 5.5h.01M3.75 10h.01M3.75 14.5h.01"/>',
    folder: '<path d="M2.75 6.25a1.5 1.5 0 0 1 1.5-1.5h3.4l1.75 2h6.35a1.5 1.5 0 0 1 1.5 1.5v6.5a1.5 1.5 0 0 1-1.5 1.5H4.25a1.5 1.5 0 0 1-1.5-1.5v-8.5Z"/>',
    user: '<circle cx="10" cy="7" r="3.25"/><path d="M3.9 16.75c.95-2.85 3.25-4.25 6.1-4.25s5.15 1.4 6.1 4.25"/>',
    settings: '<path d="M3.75 6h6.5M13.25 6h3M3.75 14h3M9.75 14h6.5"/><circle cx="11.75" cy="6" r="1.75"/><circle cx="8.25" cy="14" r="1.75"/>',
    key: '<circle cx="6.75" cy="13.25" r="3.25"/><path d="m9.1 10.9 7.15-7.15M13.75 6.25l2 2M11.75 8.25l1.5 1.5"/>',
    bolt: '<path d="M11 2.5 4.5 11.25h5l-1 6.25 6.5-8.75h-5l1-6.25Z"/>',
    arrowright: '<path d="M4 10h12M11.25 5.25 16 10l-4.75 4.75"/>',
    arrow: '<path d="m7.75 5 5 5-5 5"/>',
    chevrondown: '<path d="m5.5 8 4.5 4.5L14.5 8"/>',
    external: '<path d="M11.5 3.75h4.75V8.5M16 4l-7.25 7.25M14.25 11.5v3.25a1.5 1.5 0 0 1-1.5 1.5h-7.5a1.5 1.5 0 0 1-1.5-1.5v-7.5a1.5 1.5 0 0 1 1.5-1.5H8.5"/>',
    copy: '<rect x="7" y="7" width="9.25" height="9.25" rx="1.75"/><path d="M13 7V5.25a1.5 1.5 0 0 0-1.5-1.5H5.25a1.5 1.5 0 0 0-1.5 1.5v6.25a1.5 1.5 0 0 0 1.5 1.5H7"/>',
    search: '<circle cx="8.75" cy="8.75" r="5"/><path d="m12.5 12.5 4 4"/>',
    command: '<path d="M7.25 7.25h5.5v5.5h-5.5z"/><path d="M7.25 7.25V5.5A1.75 1.75 0 1 0 5.5 7.25h1.75ZM12.75 7.25h1.75a1.75 1.75 0 1 0-1.75-1.75v1.75ZM12.75 12.75v1.75a1.75 1.75 0 1 0 1.75-1.75h-1.75ZM7.25 12.75H5.5a1.75 1.75 0 1 0 1.75 1.75v-1.75Z"/>',
    spinner: '<path d="M10 3.75a6.25 6.25 0 1 0 6.25 6.25" class="tf-spin"/>',
    menu: '<path d="M3.5 6h13M3.5 10h13M3.5 14h13"/>',
    refresh: '<path d="M16.25 10a6.25 6.25 0 1 1-1.85-4.45M16.25 3.75v3.5h-3.5"/>',
    grid: '<rect x="3.25" y="3.25" width="5.5" height="5.5" rx="1.25"/><rect x="11.25" y="3.25" width="5.5" height="5.5" rx="1.25"/><rect x="3.25" y="11.25" width="5.5" height="5.5" rx="1.25"/><rect x="11.25" y="11.25" width="5.5" height="5.5" rx="1.25"/>',
    inbox: '<path d="M2.75 11.25h4l1.25 2h4l1.25-2h4"/><path d="M4.6 4.75h10.8l1.85 6.5v4a1.5 1.5 0 0 1-1.5 1.5H4.25a1.5 1.5 0 0 1-1.5-1.5v-4l1.85-6.5Z"/>',
    plug: '<path d="M7 2.75v3.5M13 2.75v3.5M5 6.25h10v2.5a5 5 0 0 1-10 0v-2.5ZM10 13.75v3.5"/>',
    flask: '<path d="M7.75 2.75h4.5M8.5 2.75v5L4.1 15.1a1.5 1.5 0 0 0 1.3 2.15h9.2a1.5 1.5 0 0 0 1.3-2.15L11.5 7.75v-5M6.1 12.25h7.8"/>',
    play: '<path d="M6.5 4.6v10.8a.6.6 0 0 0 .9.5l8.6-5.4a.6.6 0 0 0 0-1L7.4 4.1a.6.6 0 0 0-.9.5Z"/>',
    book: '<path d="M4 4.25a1.5 1.5 0 0 1 1.5-1.5H16v12.5H5.5A1.5 1.5 0 0 0 4 16.75V4.25Z"/><path d="M4 16.75a1.5 1.5 0 0 0 1.5 1.5H16M7.25 6.25h5.5"/>',
    signout: '<path d="M8 3.75H5.25a1.5 1.5 0 0 0-1.5 1.5v9.5a1.5 1.5 0 0 0 1.5 1.5H8M12.75 13.75 16.5 10l-3.75-3.75M16.5 10H7.75"/>',
    signin: '<path d="M12 3.75h2.75a1.5 1.5 0 0 1 1.5 1.5v9.5a1.5 1.5 0 0 1-1.5 1.5H12M8.25 13.75 12 10 8.25 6.25M12 10H3.25"/>',
    clock: '<circle cx="10" cy="10" r="7.25"/><path d="M10 6v4l2.75 1.75"/>',
    layers: '<path d="M10 2.75 17.25 6.5 10 10.25 2.75 6.5 10 2.75Z"/><path d="m2.75 10 7.25 3.75L17.25 10M2.75 13.5 10 17.25l7.25-3.75"/>',
    trendup: '<path d="m3.25 13.75 4.5-4.5 3 3 6-6M12.25 6.25h4.5v4.5"/>',
    trenddown: '<path d="m3.25 6.25 4.5 4.5 3-3 6 6M12.25 13.75h4.5v-4.5"/>',
    stop: '<circle cx="10" cy="10" r="7.25"/><path d="m5 5 10 10"/>',
    code: '<path d="m7 6-4 4 4 4M13 6l4 4-4 4"/>',
    download: '<path d="M10 3.25v9.5M6 9l4 4 4-4M3.75 16.5h12.5"/>',
    sunmoon: '<circle cx="10" cy="10" r="3.25"/><path d="M10 2.75v1.5M10 15.75v1.5M17.25 10h-1.5M4.25 10h-1.5M15.1 4.9l-1.05 1.05M5.95 14.05 4.9 15.1M15.1 15.1l-1.05-1.05M5.95 5.95 4.9 4.9"/>'
  };
  var ICON_ALIASES = { 'arrow-right': 'arrowright', 'chevron-down': 'chevrondown', 'eye-off': 'eyeoff', 'sign-out': 'signout', 'sign-in': 'signin', 'sun-moon': 'sunmoon', 'trend-up': 'trendup', 'trend-down': 'trenddown' };

  function iconMarkup(name, size, className) {
    var key = ICON_ALIASES[name] || name;
    if (!Object.prototype.hasOwnProperty.call(ICON_PATHS, key)) return '';
    var px = Math.max(8, Math.min(64, Number(size) || 16));
    var cls = 'tf-icon' + (className ? ' ' + String(className).replace(/[^A-Za-z0-9 _-]/g, '') : '');
    return '<svg class="' + cls + '" viewBox="0 0 20 20" width="' + px + '" height="' + px + '" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + ICON_PATHS[key] + '</svg>';
  }

  // Markup built from the fixed table above, never from a caller's string.
  function icon(name, size, className) { return new SafeHtml(iconMarkup(name, size, className)); }

  // A static page writes <span data-tf-icon="terminal" data-tf-size="16"></span>
  // and the icon is drawn into it here, so the set stays defined in one place.
  function drawIcons(scope) {
    all(scope || doc, '[data-tf-icon]').forEach(function (node) {
      if (node.getAttribute('data-tf-drawn') === '1') return;
      setHtml(node, icon(node.getAttribute('data-tf-icon'), node.getAttribute('data-tf-size') || 16, node.getAttribute('data-tf-class') || ''));
      node.setAttribute('data-tf-drawn', '1');
    });
  }

  // The strings the pages already use with raw(T.ICONS.x), at the sizes they
  // were drawn at, and every other icon at 16px.
  var ICONS = {};
  Object.keys(ICON_PATHS).forEach(function (name) { ICONS[name] = iconMarkup(name, 16); });
  ICONS.lock = iconMarkup('lock', 14);
  ICONS.arrow = iconMarkup('arrow', 12);
  ICONS.menu = iconMarkup('menu', 18);
  ICONS.refresh = iconMarkup('refresh', 14);

  // ----------------------------------------------------------------- theme
  //
  // The pages take Tailwind's utilities from the Play CDN. Its grays are
  // re-stepped here to the ink the tokens use, so a page's bg-gray-900 is the
  // card surface and its text-gray-500 is the muted text, which clears WCAG AA
  // (4.5:1) on every surface — Tailwind's own #6b7280 is 3.8:1 on a card. Applied
  // when this file loads, so a page sets its own tailwind.config before it.

  var INK_GRAY = {
    50: '#f6f7fb', 100: '#eceef5', 200: '#dde1ea', 300: '#c9cfdd', 400: '#a7b0c6', 500: '#8a93ab',
    600: '#818ba5', 700: '#2a3450', 800: '#1c2438', 900: '#0e1322', 950: '#080b15'
  };

  function applyTailwindTheme() {
    var tw = root.tailwind;
    if (!tw || typeof tw !== 'object') return false;
    try {
      var config = tw.config && typeof tw.config === 'object' ? tw.config : {};
      var theme = config.theme || {};
      var extend = theme.extend || {};
      var colors = extend.colors || {};
      var merged = {};
      Object.keys(config).forEach(function (k) { merged[k] = config[k]; });
      merged.darkMode = config.darkMode || 'class';
      merged.theme = {};
      Object.keys(theme).forEach(function (k) { merged.theme[k] = theme[k]; });
      merged.theme.extend = {};
      Object.keys(extend).forEach(function (k) { merged.theme.extend[k] = extend[k]; });
      merged.theme.extend.colors = {};
      Object.keys(colors).forEach(function (k) { merged.theme.extend.colors[k] = colors[k]; });
      merged.theme.extend.colors.gray = Object.assign({}, INK_GRAY, colors.gray || {});
      merged.theme.extend.fontFamily = Object.assign({
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'monospace']
      }, extend.fontFamily || {});
      tw.config = merged;
      return true;
    } catch (err) {
      return false;
    }
  }
  applyTailwindTheme();

  // ---------------------------------------------------------------- motion
  //
  // Motion with a purpose, and none at all for a reader who asked for less:
  // every helper here is a no-op under prefers-reduced-motion, and the markup
  // it is handed is already in its final state, so nothing depends on it.

  function reducedMotion() {
    try { return !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (err) { return false; }
  }
  function canAnimate() { return !reducedMotion() && typeof root.requestAnimationFrame === 'function'; }

  var countedKeys = {};

  // Counts a number up to `to` once, on first paint. The element already
  // holds the final value, written by the page, and the last frame writes
  // exactly that text back: the climb is a replay, never a number of its own.
  // If the page writes the element again mid-climb, the climb stops and the
  // page's words stand. A tab in the background, where frames do not run,
  // is left alone. options: {format, duration, key}; a key is counted once
  // per page load, so a view drawn again every thirty seconds does not climb.
  function countUp(el, to, options) {
    options = options || {};
    if (!el || !isNumber(to)) return false;
    var key = options.key ? String(options.key) : '';
    if (key && countedKeys[key]) return false;
    if (key) countedKeys[key] = true;
    if (!canAnimate() || to <= 0) return false;
    if (doc && doc.visibilityState === 'hidden') return false;
    var finalText = String(el.textContent == null ? '' : el.textContent);
    var whole = Math.round(to) === to;
    var format = typeof options.format === 'function'
      ? options.format
      : (finalText === num(to) ? num : function (v) { return whole ? Math.round(v).toLocaleString('en-US') : String(v); });
    var duration = Math.max(200, Number(options.duration) || 700);
    var start = null;
    var wrote = format(0);
    function step(now) {
      // Someone else wrote here since the last frame: theirs is the truth.
      if (el.textContent !== wrote) return;
      if (start === null) start = now;
      var t = Math.min(1, (now - start) / duration);
      if (t >= 1) { el.textContent = finalText; return; }
      var value = to * (1 - Math.pow(1 - t, 3));
      wrote = format(whole ? Math.round(value) : value);
      el.textContent = wrote;
      root.requestAnimationFrame(step);
    }
    el.textContent = wrote;
    root.requestAnimationFrame(step);
    return true;
  }

  // Every [data-tf-count] inside `scope`, counted up once each.
  function animateNumbers(scope) {
    if (!canAnimate()) return 0;
    var n = 0;
    all(scope || doc, '[data-tf-count]').forEach(function (node) {
      var to = Number(node.getAttribute('data-tf-count'));
      var key = node.getAttribute('data-tf-count-key') || node.getAttribute('data-metric') || node.id || '';
      if (countUp(node, to, { key: key || null })) n += 1;
    });
    return n;
  }

  function toList(nodes) {
    if (!nodes) return [];
    if (nodes.nodeType === 1) return [nodes];
    return Array.prototype.slice.call(nodes);
  }

  // Cards or rows fading and rising in, one after another.
  function reveal(nodes, options) {
    options = options || {};
    if (!canAnimate()) return 0;
    var stagger = isNumber(options.stagger) ? options.stagger : 45;
    var cap = isNumber(options.max) ? options.max : 10;
    var list = toList(nodes).filter(function (node) { return node && node.classList && node.style; });
    list.forEach(function (node, i) {
      node.style.setProperty('--tf-delay', (Math.min(i, cap) * stagger) + 'ms');
      node.classList.remove('tf-reveal');
      void node.offsetWidth;
      node.classList.add('tf-reveal');
    });
    return list.length;
  }

  // A ring that swells once around an element whose state just changed: a
  // label applied, a project promoted. tone: 'ok', 'danger', 'warn' or none.
  function pulse(el, tone) {
    if (!el || !el.classList || !canAnimate()) return false;
    var classes = ['tf-pulse', 'tf-pulse-ok', 'tf-pulse-danger', 'tf-pulse-warn'];
    classes.forEach(function (c) { el.classList.remove(c); });
    void el.offsetWidth;
    el.classList.add('tf-pulse');
    if (tone === 'ok' || tone === 'danger' || tone === 'warn') el.classList.add('tf-pulse-' + tone);
    setTimeout(function () { classes.forEach(function (c) { el.classList.remove(c); }); }, 1200);
    return true;
  }

  // ------------------------------------------------------------ components

  var STATUS = {
    refused: { chip: 'tf-chip-rose', icon: 'stop', word: 'Refused' },
    observed: { chip: 'tf-chip-amber', icon: 'eye', word: 'Would refuse' },
    approved: { chip: 'tf-chip-emerald', icon: 'check', word: 'Approved' },
    info: { chip: 'tf-chip-sky', icon: 'info', word: 'Note' }
  };

  // A status as an icon and a word, never colour alone.
  function statusChip(kind, word, title) {
    var s = STATUS[kind] || STATUS.info;
    return html`<span class="tf-chip ${s.chip}" title="${title || word || s.word}">${icon(s.icon, 13)}${word || s.word}</span>`;
  }

  // Keyboard hint chips: kbd('Ctrl', 'K').
  function kbd() {
    var keys = Array.prototype.slice.call(arguments);
    return html`<span class="tf-kbd-group">${keys.map(function (k) { return html`<kbd class="tf-kbd">${k}</kbd>`; })}</span>`;
  }

  // An empty state: an icon, one sentence, one action.
  // {icon, text, action: {label, href | action, primary}, state}
  function emptyState(o) {
    o = o || {};
    var a = o.action;
    var cls = 'tf-btn tf-btn-sm' + (a && a.primary ? ' tf-btn-primary' : '');
    var button = !a ? '' : a.href
      ? html`<a class="${cls}" href="${a.href}">${a.label}${icon('arrowright', 14)}</a>`
      : html`<button type="button" class="${cls}" data-action="${a.action || ''}">${a.label}</button>`;
    return html`<div class="tf-empty" data-state="${o.state || 'empty'}">
      <span class="tf-empty-icon" aria-hidden="true">${icon(o.icon || 'sparkles', 20)}</span>
      <p class="tf-empty-text">${o.text || ''}</p>
      ${button}
    </div>`;
  }

  // Grey bars in the shape of what is coming.
  function skeleton(lines, options) {
    options = options || {};
    var n = Math.max(1, Math.min(20, Number(lines) || 3));
    var rows = [];
    for (var i = 0; i < n; i++) rows.push(html`<span class="tf-skeleton tf-skeleton-line" style="width:${i === n - 1 ? 60 : 100 - (i % 3) * 8}%"></span>`);
    return html`<div class="tf-skeleton-stack" aria-hidden="true">${options.title ? html`<span class="tf-skeleton" style="height:20px;width:40%"></span>` : ''}${rows}</div>`;
  }

  // A change against a named period. up is good or bad by the caller's say.
  // {value, period, upIsGood}
  function delta(d) {
    if (!d || !isNumber(d.value)) return '';
    var up = d.value > 0;
    var flat = d.value === 0;
    var good = flat ? null : (up === (d.upIsGood !== false));
    var cls = 'tf-delta' + (good === true ? ' tf-delta-good' : good === false ? ' tf-delta-bad' : '');
    var sign = up ? '+' : (flat ? '±' : '−');
    return html`<span class="${cls}">${flat ? '' : icon(up ? 'trendup' : 'trenddown', 13)}${sign}${num(Math.abs(d.value))}${d.period ? ' ' + d.period : ''}</span>`;
  }

  // A stat tile: the plain-language label, the value, a sub-line saying what it
  // means, the precise term quieter, and optionally a sparkline, a delta and the
  // rows behind it. {label, value, format, sub, term, icon, spark, delta, href,
  // opens, metric}. The value is the API's, formatted; data-tf-count lets
  // animateNumbers() replay the climb once.
  function statTile(t) {
    t = t || {};
    var format = typeof t.format === 'function' ? t.format : num;
    var shown = format(t.value);
    var countable = isNumber(t.value) && format === num;
    var value = html`<span class="tf-tile-value"${t.metric ? html` data-metric="${t.metric}"` : ''}${countable ? html` data-tf-count="${t.value}"` : ''} title="${isNumber(t.value) ? t.value.toLocaleString('en-US') : ''}">${shown}</span>`;
    var inner = html`<span class="tf-tile-label">${t.icon ? icon(t.icon, 16) : ''}${t.label || ''}</span>
      <span class="tf-tile-row">${value}${t.spark ? html`<span class="tf-tile-spark" aria-hidden="true">${t.spark}</span>` : ''}</span>
      ${t.delta ? delta(t.delta) : ''}
      ${t.sub ? html`<span class="tf-tile-sub">${t.sub}</span>` : ''}
      ${t.term ? html`<span class="tf-tile-term">${t.term}</span>` : ''}
      ${t.href ? html`<span class="tf-tile-more" aria-hidden="true">${t.opens || 'See the rows'}${icon('arrowright', 14)}</span>` : ''}`;
    if (!t.href) return html`<div class="tf-card tf-tile">${inner}</div>`;
    return html`<a href="${t.href}" class="tf-card tf-card-link tf-tile" aria-label="${t.label}: ${shown}. ${t.sub || ''} ${t.opens || 'Open the rows behind it'}">${inner}</a>`;
  }

  // A button's loading state: aria-busy shows the spinner and holds the width.
  function setBusy(button, busy) {
    if (!button || typeof button.setAttribute !== 'function') return;
    if (busy) {
      button.setAttribute('aria-busy', 'true');
      button.disabled = true;
    } else {
      if (typeof button.removeAttribute === 'function') button.removeAttribute('aria-busy');
      else button.setAttribute('aria-busy', 'false');
      button.disabled = false;
    }
  }

  // ------------------------------------------------------------- clipboard

  function copyText(text, button) {
    var done = function (ok) {
      if (!button) return;
      var label = one(button, '[data-tf-copy-label]');
      var target = label || button;
      var original = target.getAttribute('data-label') || target.textContent;
      if (!target.getAttribute('data-label') && target.setAttribute) target.setAttribute('data-label', original);
      target.textContent = ok ? 'Copied' : 'Select and copy';
      if (button.classList) button.classList.toggle('is-copied', !!ok);
      announce(ok ? 'Copied to the clipboard.' : 'This browser would not copy. Select the text and copy it.');
      if (button.__tfCopied) clearTimeout(button.__tfCopied);
      button.__tfCopied = setTimeout(function () {
        target.textContent = original;
        if (button.classList) button.classList.remove('is-copied');
      }, 1600);
    };
    var nav = root.navigator;
    if (nav && nav.clipboard && typeof nav.clipboard.writeText === 'function') {
      nav.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
    } else {
      done(false);
    }
  }

  // A command a reader copies. The text is what they run, so it is shown whole,
  // selectable, with a copy button beside it that says when it has copied.
  function commandBlock(command, label) {
    return html`<div class="tf-command">
      <pre class="tf-pre" aria-label="${label || 'Command'}"><code>${command}</code></pre>
      <button type="button" class="tf-copy" data-tf-copy="${command}" aria-label="Copy ${label || 'the command'}">${icon('copy', 14, 'tf-icon-idle')}${icon('check', 14, 'tf-icon-done')}<span data-tf-copy-label>Copy</span></button>
    </div>`;
  }

  // ----------------------------------------------------------------- toast

  var toastSeq = 0;
  var toastActions = {};
  var toastTimer = null;

  // A message that arrives later, read out once. {tone: 'ok'|'warn'|'danger',
  // action: {label, run}, timeout}. One at a time; the newest replaces it.
  function toast(message, options) {
    options = options || {};
    var host = ownNode('tf-toast-root', 'tf-toast-root', { role: 'status', 'aria-live': 'polite' });
    if (!host) return null;
    toastSeq += 1;
    var id = 'tf-toast-' + toastSeq;
    toastActions = {};
    if (options.action && typeof options.action.run === 'function') toastActions[id] = options.action.run;
    var tone = options.tone === 'ok' || options.tone === 'warn' || options.tone === 'danger' ? options.tone : '';
    var glyph = tone === 'ok' ? 'check' : tone === 'danger' ? 'alert' : tone === 'warn' ? 'eye' : 'info';
    setHtml(host, html`<div class="tf-toast${tone ? ' tf-toast-' + tone : ''}" id="${id}">
      ${icon(glyph, 18)}<span class="tf-toast-text">${message}</span>
      ${options.action ? html`<button type="button" class="tf-btn tf-btn-sm" data-tf-toast-action="${id}">${options.action.label}</button>` : ''}
      <button type="button" class="tf-btn tf-btn-ghost tf-btn-sm" data-tf-toast-close="${id}" aria-label="Dismiss">${icon('x', 14)}</button>
    </div>`);
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { dismissToast(id); }, isNumber(options.timeout) ? options.timeout : 7000);
    return id;
  }

  function dismissToast(id) {
    var host = doc && doc.getElementById ? doc.getElementById('tf-toast-root') : null;
    if (!host) return;
    if (id && !doc.getElementById(id)) return;
    delete toastActions[id];
    setHtml(host, html``);
  }

  // ---------------------------------------------------------------- dialog

  var dialogState = null;

  // A dialog: focus moves in, stays in, and returns where it came from; Escape
  // and the backdrop close it. {title, body, footer, onClose}
  function openDialog(o) {
    o = o || {};
    closeDialog();
    var host = ownNode('tf-dialog-root');
    if (!host) return null;
    dialogState = { returnFocus: doc.activeElement || null, onClose: o.onClose || null };
    setHtml(host, html`<div class="tf-overlay" data-tf-dialog-overlay>
      <div class="tf-dialog" id="tf-dialog" role="dialog" aria-modal="true" aria-labelledby="tf-dialog-title" tabindex="-1">
        <div class="tf-dialog-head">
          <h2 id="tf-dialog-title" class="tf-dialog-title">${o.title || ''}</h2>
          <button type="button" class="tf-btn tf-btn-ghost tf-btn-sm" data-tf-dialog-close aria-label="Close">${icon('x', 16)}</button>
        </div>
        <div class="tf-dialog-body">${o.body || ''}</div>
        ${o.footer ? html`<div class="tf-dialog-foot">${o.footer}</div>` : ''}
      </div>
    </div>`);
    lockScroll(true);
    var dialog = doc.getElementById('tf-dialog');
    var first = focusables(dialog).filter(function (n) { return !n.hasAttribute('data-tf-dialog-close'); })[0];
    focusNode(first || dialog);
    return dialog;
  }

  function closeDialog() {
    if (!dialogState) return false;
    var state = dialogState;
    dialogState = null;
    var host = doc.getElementById('tf-dialog-root');
    if (host) setHtml(host, html``);
    lockScroll(false);
    focusNode(state.returnFocus);
    if (typeof state.onClose === 'function') { try { state.onClose(); } catch (err) { /* the page's own problem */ } }
    return true;
  }

  function lockScroll(on) {
    var el = doc && doc.documentElement;
    if (el && el.classList) el.classList.toggle('tf-lock', !!on);
  }

  // What Tab can reach. An element taken out of the order with tabindex="-1"
  // (the palette's rows, reached by the arrow keys) is not one of them.
  var FOCUSABLE = [
    'a[href]', 'button:not([disabled])', 'input:not([disabled]):not([type="hidden"])',
    'select:not([disabled])', 'textarea:not([disabled])', '[tabindex]'
  ].map(function (s) { return s + ':not([tabindex="-1"])'; }).join(', ');

  function focusables(container) {
    return all(container, FOCUSABLE).filter(function (n) {
      return typeof n.getClientRects !== 'function' || n.getClientRects().length > 0;
    });
  }

  // Tab and Shift+Tab stay inside whatever is modal: the palette, a dialog of
  // this layer's, or a page's own [aria-modal="true"].
  function trapFocus(event) {
    var modal = null;
    if (palette.open) modal = doc.getElementById('tf-palette');
    else {
      var open = all(doc, '[aria-modal="true"]');
      modal = open.length ? open[open.length - 1] : null;
    }
    if (!modal) return;
    var nodes = focusables(modal);
    if (!nodes.length) { event.preventDefault(); focusNode(modal); return; }
    var first = nodes[0];
    var last = nodes[nodes.length - 1];
    var active = doc.activeElement;
    if (!within(active, modal)) { event.preventDefault(); focusNode(first); return; }
    if (event.shiftKey && active === first) { event.preventDefault(); focusNode(last); }
    else if (!event.shiftKey && active === last) { event.preventDefault(); focusNode(first); }
  }

  // ------------------------------------------------------------------ tabs
  //
  // <div data-tf-tabs role="tablist"> of <button role="tab" aria-controls>:
  // a click or the arrow keys select, Home and End jump, and the panel of each
  // other tab is hidden.

  function selectTab(tab, focus) {
    if (!tab || typeof tab.getAttribute !== 'function') return false;
    var list = closestAttr(tab.parentNode, 'data-tf-tabs') || closestAttr(tab, 'data-tf-tabs');
    var tabs = list ? all(list, '[role="tab"]') : [tab];
    tabs.forEach(function (t) {
      var on = t === tab;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.setAttribute('tabindex', on ? '0' : '-1');
      var panel = doc.getElementById(t.getAttribute('aria-controls'));
      if (panel) panel.hidden = !on;
    });
    if (focus) focusNode(tab);
    return true;
  }

  function tabKeys(event, tab) {
    var list = closestAttr(tab, 'data-tf-tabs');
    var tabs = list ? all(list, '[role="tab"]') : [];
    if (!tabs.length) return;
    var i = tabs.indexOf(tab);
    var next = null;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = tabs[(i + 1) % tabs.length];
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = tabs[(i - 1 + tabs.length) % tabs.length];
    else if (event.key === 'Home') next = tabs[0];
    else if (event.key === 'End') next = tabs[tabs.length - 1];
    if (!next) return;
    event.preventDefault();
    selectTab(next, true);
  }

  // ------------------------------------------------------------ navigation
  //
  // Five destinations a reader goes to every day, and five they reach now and
  // then under More. The same items make the menu sheet on a phone and the
  // first group of the command palette.

  var NAV = [
    { id: 'overview', label: 'Overview', page: 'dashboard.html', hash: '#/overview', icon: 'grid', primary: true, hint: 'What every agent’s calls came to, across projects' },
    { id: 'review', label: 'Review', page: 'dashboard.html', hash: '#/review', icon: 'inbox', primary: true, hint: 'Calls a rule would have stopped, waiting for your label' },
    { id: 'projects', label: 'Projects', page: 'dashboard.html', hash: '#/projects', icon: 'folder', primary: true, hint: 'Each repository, its stage and how ready its rules are' },
    { id: 'rules', label: 'Rules', page: 'rules.html', icon: 'shield', primary: true, hint: 'What each layer of the code may not depend on' },
    { id: 'sessions', label: 'Sessions', page: 'sessions.html', icon: 'terminal', primary: true, hint: 'Every agent session the service has judged' },
    { id: 'connect', label: 'Connect', page: 'dashboard.html', hash: '#/connect', icon: 'plug', hint: 'Govern a repository with one command' },
    { id: 'proof', label: 'Proof', page: 'dashboard.html', hash: '#/proof', icon: 'flask', hint: 'The benchmark: agents with and without Threefold' },
    { id: 'demo', label: 'Demo', page: 'index.html', icon: 'play', hint: 'The flagship scenarios, no account needed' },
    { id: 'api', label: 'API', page: 'swagger.html', icon: 'book', hint: 'The published OpenAPI document' },
    { id: 'settings', label: 'Settings', page: 'settings.html', icon: 'settings', hint: 'Sign-in, the policy and your own names for projects' }
  ];

  function navHref(item, inDashboard) {
    if (item.hash && inDashboard) return item.hash;
    return PAGE_BASE + item.page + (item.hash || '');
  }

  var mounted = [];

  // Draws the shared navigation into `el`: the primary links and More, the
  // search that opens the command palette, what kind of stack this is, the
  // switch for the reader's own names, and whether this browser is signed in.
  // On a narrow screen the links fold into a menu sheet. `active` is the id of
  // the page or route the reader is on; `inDashboard` makes the dashboard's own
  // routes plain hash links.
  function mountNav(el, options) {
    if (!el) return;
    options = options || {};
    var entry = null;
    for (var i = 0; i < mounted.length; i++) if (mounted[i].el === el) entry = mounted[i];
    if (!entry) {
      entry = { el: el, options: options, index: mounted.length };
      mounted.push(entry);
    }
    entry.options = options;
    drawNav(entry, null);
    linkPages(doc);
    drawIcons(doc);
    whoami().then(function (who) { drawNav(entry, who); });
  }

  function navLink(l, cls) {
    return html`<a href="${l.href}" class="${cls}${l.current ? ' tf-nav-current' : ''}"${l.current ? raw(' aria-current="page"') : ''}>${l.item.label}</a>`;
  }

  function menuLink(l, cls) {
    return html`<a href="${l.href}" class="${cls}"${l.current ? raw(' aria-current="page"') : ''}>${icon(l.item.icon, 16)}${l.item.label}</a>`;
  }

  function drawNav(entry, who) {
    var options = entry.options;
    // Drawn again on a sign-in, a route change or the names switch: a sheet
    // left open is closed first, so the page under it does not stay locked.
    var sheet = doc && doc.getElementById ? doc.getElementById('tf-menu') : null;
    if (sheet && sheet.classList && sheet.classList.contains && !sheet.classList.contains('hidden') && within(sheet, entry.el)) toggleMenu(null, false);
    var links = NAV.map(function (item) {
      return { item: item, current: item.id === options.active, href: navHref(item, options.inDashboard) };
    });
    var primary = links.filter(function (l) { return l.item.primary; });
    var more = links.filter(function (l) { return !l.item.primary; });
    var moreCurrent = more.some(function (l) { return l.current; });
    var menuId = 'tf-more-' + entry.index;
    var mod = isMac() ? '⌘' : 'Ctrl';
    setHtml(entry.el, html`<div class="tf-shell">
      <nav aria-label="Threefold" class="tf-nav">
        ${primary.map(function (l) { return navLink(l, 'tf-nav-link'); })}
        <div class="tf-more" data-tf-more>
          <button type="button" class="tf-nav-link${moreCurrent ? ' tf-nav-current' : ''}" data-tf-more-toggle aria-expanded="false" aria-controls="${menuId}">More${moreCurrent ? html`<span class="tf-nav-dot" aria-hidden="true"></span>` : ''}${icon('chevrondown', 14, 'tf-chevron')}</button>
          <div class="tf-popover" id="${menuId}" data-tf-more-menu hidden>
            ${more.map(function (l) { return menuLink(l, 'tf-menu-item'); })}
          </div>
        </div>
      </nav>
      <div class="tf-tools">
        <button type="button" class="tf-cmdk" data-tf-palette-open aria-haspopup="dialog" aria-keyshortcuts="Control+K Meta+K /" aria-label="Search pages, projects and actions (${mod}+K or /)">${icon('search', 16)}<span class="tf-cmdk-words">Search</span><span class="tf-kbd-group" aria-hidden="true"><kbd class="tf-kbd">${mod}</kbd><kbd class="tf-kbd">K</kbd></span></button>
        <div class="tf-tools-wide">${localNamesToggle()}</div>
        ${authChips(who, false)}
        <button type="button" class="tf-icon-btn tf-menu-btn" data-tf-menu aria-expanded="false" aria-controls="tf-menu" aria-label="Open the menu">${icon('menu', 18)}</button>
      </div>
      <div id="tf-menu" class="tf-sheet hidden">
        <nav aria-label="Threefold, menu">
          <div class="tf-sheet-section">
            <p class="tf-sheet-title">Go to</p>
            <div class="tf-sheet-grid">${primary.map(function (l) { return menuLink(l, 'tf-sheet-link'); })}</div>
          </div>
          <div class="tf-sheet-section">
            <p class="tf-sheet-title">More</p>
            <div class="tf-sheet-grid">${more.map(function (l) { return menuLink(l, 'tf-sheet-link'); })}</div>
          </div>
        </nav>
        <div class="tf-sheet-row">${localNamesToggle()}${authChips(who, true)}</div>
      </div>
    </div>`);
  }

  // The stack kind comes from the stack; the sign-in from the stack when it
  // answers whoami, and from this browser alone when it does not. In the bar on
  // a narrow screen only the sign-in shows; the rest moves into the menu, which
  // is drawn with `inMenu` set.
  function authChips(who, inMenu) {
    var chips = [];
    var wide = function (chip) { return inMenu ? chip : html`<span class="tf-wide-only">${chip}</span>`; };
    if (who && who.reads_public === true) {
      chips.push(wide(html`<span class="tf-chip tf-chip-sky" title="Anyone can read this stack's ledger: it is the public demo">${icon('eye', 13)}Public demo</span>`));
    } else if (who && who.reads_public === false) {
      chips.push(wide(html`<span class="tf-chip tf-chip-gray" title="This stack's ledger is read only by its operator">${icon('lock', 13)}Private stack</span>`));
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
      var until = isNaN(ends) ? '' : ' · until ' + clockTime(ends);
      if (!inMenu) chips.push(html`<span class="tf-chip tf-chip-emerald" title="Signed in to this stack from the command line${until}">${icon('check', 13)}Signed in<span class="tf-wide-only">${until}</span></span>`);
      else chips.push(html`<span class="tf-chip tf-chip-emerald">${icon('check', 13)}Signed in${until}</span>`);
      chips.push(wide(html`<button type="button" class="tf-chip tf-chip-button" data-tf-signout>${icon('signout', 13)}Sign out</button>`));
    } else if (via === 'key') {
      chips.push(html`<a href="${PAGE_BASE}settings.html#operator-key" class="tf-chip tf-chip-amber" title="Using the operator key pasted on the settings page; signing in with python threefold.py open replaces it">${icon('key', 13)}Operator key</a>`);
    } else if (who && who.reads_public === false) {
      chips.push(html`<a href="${PAGE_BASE}dashboard.html#/signin" class="tf-chip tf-chip-amber" title="How to sign in">${icon('lock', 13)}Signed out</a>`);
    } else if (who) {
      chips.push(html`<a href="${PAGE_BASE}dashboard.html#/signin" class="tf-chip tf-chip-gray" title="The operator signs in with python threefold.py open">${icon('signin', 13)}Sign in</a>`);
    }
    // A stack that does not answer whoami says nothing about itself, so no chip
    // guesses whether it is the public demo or a private stack.
    return chips;
  }

  function redrawNavs() {
    mounted.forEach(function (entry) {
      drawNav(entry, null);
      whoami().then(function (who) { drawNav(entry, who); });
    });
  }
  onAuthChange(function () { palette.projects = null; redrawNavs(); });
  // The switch itself lives in the navigation, so flipping it redraws the bar
  // as well as the page that listened.
  onLocalNamesChange(function () { redrawNavs(); if (palette.open) renderPaletteList(); });

  function toggleMenu(button, open) {
    var menu = doc.getElementById('tf-menu');
    if (!menu || !menu.classList) return;
    var show = open === undefined ? menu.classList.contains('hidden') : open;
    var was = !menu.classList.contains('hidden');
    menu.classList.toggle('hidden', !show);
    // The sheet covers the page, so the page under it holds still.
    if (show !== was && !palette.open && !dialogState) lockScroll(show);
    var toggle = button || one(doc, '[data-tf-menu]');
    if (toggle && toggle.setAttribute) {
      toggle.setAttribute('aria-expanded', show ? 'true' : 'false');
      toggle.setAttribute('aria-label', show ? 'Close the menu' : 'Open the menu');
    }
    if (show) focusNode(one(menu, 'a'));
  }

  function moreMenus() { return all(doc, '[data-tf-more]'); }

  function setMore(wrap, open, focusFirst) {
    var button = one(wrap, '[data-tf-more-toggle]');
    var menu = one(wrap, '[data-tf-more-menu]');
    if (!button || !menu) return;
    menu.hidden = !open;
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && focusFirst) focusNode(one(menu, 'a'));
  }

  function closeMores(except) {
    moreMenus().forEach(function (wrap) { if (wrap !== except) setMore(wrap, false); });
  }

  function moreKeys(event, wrap) {
    var items = all(wrap, '[data-tf-more-menu] a');
    if (!items.length) return;
    var i = items.indexOf(doc.activeElement);
    var next = null;
    if (event.key === 'ArrowDown') next = items[(i + 1) % items.length];
    else if (event.key === 'ArrowUp') next = items[(i - 1 + items.length) % items.length];
    else if (event.key === 'Home') next = items[0];
    else if (event.key === 'End') next = items[items.length - 1];
    if (!next) return;
    event.preventDefault();
    var menu = one(wrap, '[data-tf-more-menu]');
    if (menu && menu.hidden) setMore(wrap, true);
    focusNode(next);
  }

  function navContext() {
    for (var i = mounted.length - 1; i >= 0; i--) if (mounted[i].options) return mounted[i].options;
    var path = (root.location && root.location.pathname) || '';
    return { inDashboard: /(dashboard\.html|\/app)$/.test(path) };
  }

  // --------------------------------------------------------------- palette
  //
  // Ctrl+K, Cmd+K or "/" opens it anywhere. It jumps to a page or a route, to
  // a project by its alias (or by the reader's own name for it, matched here
  // and never sent), and runs what a visitor came to do. Arrow keys move,
  // Enter opens, Escape closes. Its one request is GET /api/projects, made the
  // first time it opens, and a project it cannot read is simply not listed.

  var palette = { open: false, query: '', active: 0, results: [], projects: null, loading: false, returnFocus: null };

  function paletteCatalog() {
    var ctx = navContext();
    var inDash = !!ctx.inDashboard;
    var items = [];
    var route = function (page, hash) { return hash && inDash ? hash : PAGE_BASE + page + (hash || ''); };
    NAV.forEach(function (item) {
      if (item.id === 'connect' || item.id === 'proof') return;
      items.push({ group: 'Go to', id: 'go-' + item.id, text: item.label, icon: item.icon, hint: item.hint, href: navHref(item, inDash), keywords: [item.id, item.page] });
    });
    items.splice(3, 0, { group: 'Go to', id: 'go-calls', text: 'Calls', icon: 'list', hint: 'Every judged call, filtered by project, rule or agent', href: route('dashboard.html', '#/calls'), keywords: ['ledger', 'decisions'] });
    items.push({ group: 'Go to', id: 'go-names', text: 'Your own names for projects', icon: 'user', hint: 'Kept in this browser, never sent', href: route('settings.html#local-names'), keywords: ['labels', 'aliases'] });
    items.push({ group: 'Actions', id: 'do-try', text: 'Try the two-stage rollout', icon: 'sparkles', hint: 'A sandbox project: label, promote, watch a call be stopped. About a minute', href: route('dashboard.html', '#/try'), keywords: ['walkthrough', 'sandbox', 'demo', 'observe', 'enforce'] });
    items.push({ group: 'Actions', id: 'do-connect', text: 'Connect a repository', icon: 'plug', hint: 'One command. It starts in Observe, so nothing is blocked', href: route('dashboard.html', '#/connect'), keywords: ['install', 'hook', 'setup'] });
    items.push({ group: 'Actions', id: 'do-proof', text: 'Open the proof', icon: 'flask', hint: 'The benchmark: the same tasks with and without Threefold', href: route('dashboard.html', '#/proof'), keywords: ['benchmark', 'evidence'] });
    if (Object.keys(readLocalNames()).length) {
      var hidden = localNamesHidden();
      items.push({ group: 'Actions', id: 'do-names', text: hidden ? 'Show your own names' : 'Hide your own names', icon: hidden ? 'eye' : 'eyeoff', hint: hidden ? 'Beside each alias again' : 'For a screenshot or a demo', run: function () { setLocalNamesHidden(!localNamesHidden()); }, keywords: ['screenshot', 'labels'] });
    }
    if (readSession()) {
      items.push({ group: 'Actions', id: 'do-signout', text: 'Sign out', icon: 'signout', hint: 'Revoke this browser’s sign-in on the stack', run: function () { signOut(); }, keywords: ['log out', 'logout'] });
    } else {
      items.push({ group: 'Actions', id: 'do-signin', text: 'How to sign in', icon: 'signin', hint: 'python threefold.py open, on the machine that holds the key', href: route('dashboard.html', '#/signin'), keywords: ['login', 'operator'] });
    }
    (Array.isArray(palette.projects) ? palette.projects : []).forEach(function (p) {
      items.push(projectItem(p, inDash));
    });
    return items;
  }

  // A project from GET /api/projects. The link is built from the alias alone;
  // the reader's own name is only matched against and shown.
  function projectItem(p, inDash) {
    var alias = String(p.project);
    var stage = p.stage === 'enforce' ? 'Enforce' : 'Observe';
    var facts = [stage];
    if (isNumber(p.calls)) facts.push(num(p.calls) + ' calls in 7 days');
    if (isNumber(p.needs_review) && p.needs_review > 0) facts.push(num(p.needs_review) + ' to review');
    var target = '#/projects/' + encodeURIComponent(alias);
    var item = { group: 'Projects', id: 'project-' + alias, text: alias, alias: alias, icon: 'folder', hint: facts.join(' · '), href: inDash ? target : PAGE_BASE + 'dashboard.html' + target, keywords: [] };
    item.keywords.push(projectLabel(alias));
    return item;
  }

  function loadPaletteProjects() {
    if (palette.loading || Array.isArray(palette.projects)) return;
    palette.loading = true;
    api('/api/projects').then(function (body) {
      var list = body && Array.isArray(body.projects) ? body.projects : [];
      palette.projects = list.filter(function (p) { return p && typeof p.project === 'string' && p.project; });
    }, function () {
      palette.projects = [];
    }).then(function () {
      palette.loading = false;
      if (palette.open) renderPaletteList();
    });
  }

  // A forgiving match: the query's letters in order, scored higher when they
  // run together, start a word, or start the text. -1 is no match.
  function fuzzyScore(queryText, text) {
    var q = String(queryText || '').toLowerCase().replace(/\s+/g, ' ').trim();
    var t = String(text || '').toLowerCase();
    if (!q) return 0;
    if (!t) return -1;
    var at = t.indexOf(q);
    if (at !== -1) return 1000 - at * 3 + (at === 0 || /[\s\-/._·]/.test(t.charAt(at - 1)) ? 200 : 0) - Math.max(0, t.length - q.length) * 0.5;
    var score = 0;
    var from = 0;
    var prev = -2;
    var first = -1;
    for (var i = 0; i < q.length; i++) {
      var ch = q.charAt(i);
      if (ch === ' ') continue;
      var found = t.indexOf(ch, from);
      if (found === -1) return -1;
      if (first < 0) first = found;
      score += found === prev + 1 ? 12 : 1;
      if (found === 0 || /[\s\-/._·]/.test(t.charAt(found - 1))) score += 8;
      prev = found;
      from = found + 1;
    }
    return score - first * 0.5 - t.length * 0.05;
  }

  function paletteResults() {
    var q = palette.query;
    var items = paletteCatalog();
    if (!q.trim()) {
      var projects = items.filter(function (i) { return i.group === 'Projects'; }).slice(0, 6);
      return items.filter(function (i) { return i.group !== 'Projects'; }).concat(projects);
    }
    var scored = [];
    items.forEach(function (item, order) {
      var best = fuzzyScore(q, item.text);
      (item.keywords || []).forEach(function (k) { best = Math.max(best, fuzzyScore(q, k) - 5); });
      if (best >= 0) scored.push({ item: item, score: best, order: order });
    });
    scored.sort(function (a, b) { return (b.score - a.score) || (a.order - b.order); });
    return scored.slice(0, 30).map(function (s) { return s.item; });
  }

  function openPalette(initial) {
    if (!doc) return false;
    var host = ownNode('tf-palette-root');
    if (!host) return false;
    closeMores();
    toggleMenu(null, false);
    if (!palette.open) palette.returnFocus = doc.activeElement || null;
    palette.open = true;
    palette.query = typeof initial === 'string' ? initial : '';
    palette.active = 0;
    var mod = isMac() ? '⌘' : 'Ctrl';
    setHtml(host, html`<div class="tf-palette-overlay" data-tf-palette-overlay>
      <div class="tf-palette" id="tf-palette" role="dialog" aria-modal="true" aria-label="Search pages, projects and actions">
        <div class="tf-palette-search">
          ${icon('search', 20)}
          <input id="tf-palette-input" class="tf-palette-input" data-tf-palette-input type="text" role="combobox"
            aria-expanded="true" aria-controls="tf-palette-list" aria-autocomplete="list" aria-activedescendant=""
            placeholder="Jump to a page, a project or an action" autocomplete="off" autocapitalize="off" spellcheck="false" value="${palette.query}" />
          <span class="tf-kbd-group" aria-hidden="true"><kbd class="tf-kbd">Esc</kbd></span>
        </div>
        <div id="tf-palette-list" class="tf-palette-list" role="listbox" aria-label="Results"></div>
        <div class="tf-palette-foot" aria-hidden="true">
          <span><kbd class="tf-kbd">↑</kbd><kbd class="tf-kbd">↓</kbd> move</span>
          <span><kbd class="tf-kbd">Enter</kbd> open</span>
          <span><kbd class="tf-kbd">Esc</kbd> close</span>
          <span><kbd class="tf-kbd">${mod}</kbd><kbd class="tf-kbd">K</kbd> or <kbd class="tf-kbd">/</kbd> from anywhere</span>
        </div>
        <p id="tf-palette-status" class="sr-only" aria-live="polite"></p>
      </div>
    </div>`);
    lockScroll(true);
    renderPaletteList();
    focusNode(doc.getElementById('tf-palette-input'));
    loadPaletteProjects();
    return true;
  }

  function closePalette(restore) {
    if (!palette.open) return false;
    palette.open = false;
    var host = doc.getElementById('tf-palette-root');
    if (host) setHtml(host, html``);
    if (!dialogState) lockScroll(false);
    if (restore !== false) focusNode(palette.returnFocus);
    palette.returnFocus = null;
    return true;
  }

  function togglePalette() { return palette.open ? closePalette() : openPalette(); }

  function renderPaletteList() {
    if (!palette.open) return;
    var list = doc.getElementById('tf-palette-list');
    if (!list) return;
    palette.results = paletteResults();
    if (palette.active >= palette.results.length) palette.active = Math.max(0, palette.results.length - 1);
    var groups = [];
    palette.results.forEach(function (item, index) {
      var group = groups.length && groups[groups.length - 1].name === item.group ? groups[groups.length - 1] : null;
      if (!group) { group = { name: item.group, rows: [] }; groups.push(group); }
      group.rows.push({ item: item, index: index });
    });
    var q = palette.query.trim();
    setHtml(list, palette.results.length ? html`${groups.map(function (g, gi) {
      return html`<div role="group" aria-labelledby="tf-pal-g${gi}">
        <div class="tf-palette-group" id="tf-pal-g${gi}" role="presentation">${g.name}</div>
        ${g.rows.map(function (row) { return paletteRow(row.item, row.index); })}
      </div>`;
    })}` : html`<div class="tf-palette-empty">Nothing matches “${q}”. Try a page, a project alias, or “connect”.</div>`);
    var input = doc.getElementById('tf-palette-input');
    if (input && input.setAttribute) input.setAttribute('aria-activedescendant', palette.results.length ? 'tf-pal-' + palette.active : '');
    var status = doc.getElementById('tf-palette-status');
    if (status) {
      var projectsNote = palette.loading ? ' Reading projects.' : '';
      status.textContent = palette.results.length + (palette.results.length === 1 ? ' result.' : ' results.') + projectsNote;
    }
    var active = doc.getElementById('tf-pal-' + palette.active);
    if (active && typeof active.scrollIntoView === 'function') { try { active.scrollIntoView({ block: 'nearest' }); } catch (err) { /* courtesy */ } }
  }

  function paletteRow(item, index) {
    var selected = index === palette.active;
    var label = item.alias ? projectName(item.alias) : html`${item.text}`;
    var body = html`<span class="tf-palette-icon" aria-hidden="true">${icon(item.icon, 16)}</span>
      <span class="tf-palette-text"><span class="tf-palette-label">${label}</span>${item.hint ? html`<span class="tf-palette-hint">${item.hint}</span>` : ''}</span>
      <span class="tf-palette-go" aria-hidden="true">${icon(item.run ? 'arrowright' : 'arrow', 16)}</span>`;
    if (item.href) {
      return html`<a class="tf-palette-item" id="tf-pal-${index}" role="option" aria-selected="${selected ? 'true' : 'false'}" tabindex="-1" href="${item.href}" data-tf-palette-index="${index}">${body}</a>`;
    }
    return html`<div class="tf-palette-item" id="tf-pal-${index}" role="option" aria-selected="${selected ? 'true' : 'false'}" tabindex="-1" data-tf-palette-index="${index}">${body}</div>`;
  }

  function movePalette(step) {
    if (!palette.open || !palette.results.length) return;
    var n = palette.results.length;
    palette.active = step === 'first' ? 0 : step === 'last' ? n - 1 : (palette.active + step + n) % n;
    renderPaletteList();
  }

  function setPaletteQuery(text) {
    palette.query = String(text == null ? '' : text);
    palette.active = 0;
    renderPaletteList();
  }

  // Opens what the palette has selected, or the row at `index`.
  function choosePalette(index) {
    if (!palette.open) return false;
    var i = typeof index === 'number' ? index : palette.active;
    var item = palette.results[i];
    if (!item) return false;
    closePalette(!!item.run);
    if (item.run) { item.run(); return true; }
    goTo(item.href);
    return true;
  }

  function goTo(href) {
    if (!href || !root.location) return;
    if (href.charAt(0) === '#') root.location.hash = href;
    else root.location.href = href;
  }

  function isEditable(target) {
    if (!target) return false;
    if (target.isContentEditable) return true;
    var tag = String(target.tagName || '').toUpperCase();
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
  }

  // -------------------------------------------------------------- tooltip
  //
  // One tooltip for every chart mark carrying data-tf-tip (the label),
  // data-tf-tip-value, data-tf-tip-color (a line key) and data-tf-tip-action.
  // Shown on hover and on keyboard focus alike; written with textContent.

  var tipFor = null;

  function showTip(node) {
    var tip = ownNode('tf-tip', 'tf-tip', { role: 'tooltip' });
    if (!tip || !node) return;
    tipFor = node;
    var label = node.getAttribute('data-tf-tip') || '';
    var value = node.getAttribute('data-tf-tip-value') || '';
    var color = node.getAttribute('data-tf-tip-color') || '';
    var action = node.getAttribute('data-tf-tip-action') || '';
    if (typeof doc.createElement === 'function' && typeof tip.appendChild === 'function') {
      while (tip.firstChild) tip.removeChild(tip.firstChild);
      var head = doc.createElement('span');
      head.className = 'tf-tip-value';
      if (color && /^#[0-9a-fA-F]{3,8}$/.test(color)) {
        var key = doc.createElement('span');
        key.className = 'tf-tip-key';
        key.style.background = color;
        head.appendChild(key);
      }
      head.appendChild(doc.createTextNode(value || label));
      tip.appendChild(head);
      if (value && label) {
        var sub = doc.createElement('span');
        sub.className = 'tf-tip-label';
        sub.textContent = label;
        tip.appendChild(sub);
      }
      if (action) {
        var act = doc.createElement('span');
        act.className = 'tf-tip-action';
        act.textContent = action;
        tip.appendChild(act);
      }
    } else {
      tip.textContent = (value ? value + ' · ' : '') + label;
    }
    placeTip(tip, node);
    tip.setAttribute('data-show', 'true');
  }

  function placeTip(tip, node) {
    if (typeof node.getBoundingClientRect !== 'function' || !tip.style) return;
    var r = node.getBoundingClientRect();
    var w = tip.offsetWidth || 160;
    var h = tip.offsetHeight || 40;
    var vw = root.innerWidth || 1024;
    var x = Math.max(8, Math.min(vw - w - 8, r.left + r.width / 2 - w / 2));
    var y = r.top - h - 10;
    if (y < 8) y = r.bottom + 10;
    tip.style.left = Math.round(x) + 'px';
    tip.style.top = Math.round(y) + 'px';
  }

  function hideTip() {
    tipFor = null;
    var tip = doc && doc.getElementById ? doc.getElementById('tf-tip') : null;
    if (tip && tip.setAttribute) tip.setAttribute('data-show', 'false');
  }

  // ---------------------------------------------------------------- events

  if (doc && typeof doc.addEventListener === 'function') {
    doc.addEventListener('click', function (event) {
      var target = event && event.target;
      if (!target) return;
      var copy = closestAttr(target, 'data-tf-copy');
      if (copy) { copyText(copy.getAttribute('data-tf-copy'), copy); return; }
      var menu = closestAttr(target, 'data-tf-menu');
      if (menu) { toggleMenu(menu); return; }
      if (closestAttr(target, 'data-tf-names')) { setLocalNamesHidden(!localNamesHidden()); return; }
      if (closestAttr(target, 'data-tf-signout')) { signOut(); return; }
      if (closestAttr(target, 'data-tf-palette-open')) { openPalette(); return; }
      var row = closestAttr(target, 'data-tf-palette-index');
      if (row && palette.open) {
        if (event.ctrlKey || event.metaKey || event.shiftKey || event.button === 1) { closePalette(false); return; }
        if (typeof event.preventDefault === 'function') event.preventDefault();
        choosePalette(Number(row.getAttribute('data-tf-palette-index')));
        return;
      }
      if (palette.open && typeof target.hasAttribute === 'function' && target.hasAttribute('data-tf-palette-overlay')) { closePalette(); return; }
      var more = closestAttr(target, 'data-tf-more-toggle');
      if (more) {
        var wrap = closestAttr(more, 'data-tf-more');
        var opening = more.getAttribute('aria-expanded') !== 'true';
        closeMores(wrap);
        setMore(wrap, opening, false);
        return;
      }
      if (!closestAttr(target, 'data-tf-more')) closeMores();
      else if (target.closest && target.closest('[data-tf-more-menu] a')) closeMores();
      var tab = target.closest ? target.closest('[data-tf-tabs] [role="tab"]') : null;
      if (tab) { selectTab(tab, false); return; }
      var tableToggle = closestAttr(target, 'data-tf-table-toggle');
      if (tableToggle) { toggleChartTable(tableToggle); return; }
      if (closestAttr(target, 'data-tf-dialog-close')) { closeDialog(); return; }
      if (dialogState && typeof target.hasAttribute === 'function' && target.hasAttribute('data-tf-dialog-overlay')) { closeDialog(); return; }
      var toastAction = closestAttr(target, 'data-tf-toast-action');
      if (toastAction) {
        var id = toastAction.getAttribute('data-tf-toast-action');
        var run = toastActions[id];
        dismissToast(id);
        if (run) run();
        return;
      }
      var toastClose = closestAttr(target, 'data-tf-toast-close');
      if (toastClose) { dismissToast(toastClose.getAttribute('data-tf-toast-close')); return; }
      // Following a link in the folded menu closes it.
      if (target.closest && target.closest('#tf-menu a')) toggleMenu(null, false);
    });

    doc.addEventListener('keydown', function (event) {
      if (!event) return;
      var key = event.key;
      var stop = function () {
        if (typeof event.preventDefault === 'function') event.preventDefault();
        if (typeof event.stopImmediatePropagation === 'function') event.stopImmediatePropagation();
      };
      if ((key === 'k' || key === 'K') && (event.ctrlKey || event.metaKey) && !event.altKey) { stop(); togglePalette(); return; }
      if (palette.open) {
        if (key === 'Escape') { stop(); closePalette(); return; }
        if (key === 'ArrowDown') { stop(); movePalette(1); return; }
        if (key === 'ArrowUp') { stop(); movePalette(-1); return; }
        if (key === 'PageDown') { stop(); movePalette('last'); return; }
        if (key === 'PageUp') { stop(); movePalette('first'); return; }
        if (key === 'Enter') { stop(); choosePalette(); return; }
        // The rows are reached with the arrow keys; Tab stays in the field.
        if (key === 'Tab') { stop(); focusNode(doc.getElementById('tf-palette-input')); return; }
        return;
      }
      if (key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && !isEditable(event.target)) { stop(); openPalette(); return; }
      if (key === 'Escape') {
        hideTip();
        var menuOpen = doc.getElementById('tf-menu');
        var sheetWasOpen = menuOpen && menuOpen.classList && !menuOpen.classList.contains('hidden');
        toggleMenu(null, false);
        if (sheetWasOpen) focusNode(one(doc, '[data-tf-menu]'));
        var openMore = moreMenus().filter(function (w) { var m = one(w, '[data-tf-more-menu]'); return m && !m.hidden; })[0];
        if (openMore) { closeMores(); focusNode(one(openMore, '[data-tf-more-toggle]')); }
        if (dialogState) { stop(); closeDialog(); }
        return;
      }
      if (key === 'Tab') { trapFocus(event); return; }
      var target = event.target;
      var tab = target && target.closest ? target.closest('[data-tf-tabs] [role="tab"]') : null;
      if (tab) { tabKeys(event, tab); return; }
      var wrap = closestAttr(target, 'data-tf-more');
      if (wrap && (key === 'ArrowDown' || key === 'ArrowUp' || key === 'Home' || key === 'End')) moreKeys(event, wrap);
    });

    doc.addEventListener('input', function (event) {
      var target = event && event.target;
      if (target && typeof target.hasAttribute === 'function' && target.hasAttribute('data-tf-palette-input')) setPaletteQuery(target.value);
    });

    doc.addEventListener('mouseover', function (event) {
      var node = closestAttr(event && event.target, 'data-tf-tip');
      if (node && node !== tipFor) showTip(node);
      else if (!node && tipFor) hideTip();
    });
    doc.addEventListener('focusin', function (event) {
      var node = closestAttr(event && event.target, 'data-tf-tip');
      if (node) showTip(node); else if (tipFor) hideTip();
      // Focus leaving the More menu closes it.
      moreMenus().forEach(function (wrap) { if (!within(event && event.target, wrap)) setMore(wrap, false); });
    });
    doc.addEventListener('focusout', function (event) {
      if (closestAttr(event && event.target, 'data-tf-tip')) hideTip();
    });
  }
  if (doc && doc.readyState === 'loading' && typeof doc.addEventListener === 'function') {
    doc.addEventListener('DOMContentLoaded', function () { drawIcons(doc); linkPages(doc); });
  }
  if (typeof root.addEventListener === 'function') {
    root.addEventListener('scroll', function () { if (tipFor) hideTip(); }, { passive: true });
    root.addEventListener('hashchange', function () { closePalette(false); hideTip(); });
  }

  // ---------------------------------------------------------------- charts
  //
  // Inline SVG and HTML, drawn from the numbers a page is handed, to the
  // dataviz method: thin marks with a 4px round data end, square at the
  // baseline; 2px gaps in the surface colour between touching fills;
  // recessive solid hairline grid and axes; a legend for two series or more;
  // direct labels on at most a few values; a tooltip on every mark, on hover
  // and on keyboard focus; and a table view of every value, which a screen
  // reader always has and a sighted reader can open. Every mark that has rows
  // behind it links to them.

  // The status colours were validated as stacked series on the dark card (see
  // the tokens in threefold.css): refused, would refuse, approved.
  var COLORS = {
    approved: '#0ea572',
    observed: '#d97706',
    refused: '#e11d48',
    info: '#0284c7',
    accent: '#8b5cf6',
    track: '#161d31',
    grid: '#1c2438',
    baseline: '#2a3450',
    axis: '#8a93ab',
    surface: '#0e1322'
  };
  // Identity, never status: an agent, an origin. Fixed order, never cycled;
  // past three the rest fold into "other".
  var CATEGORICAL = ['#8b5cf6', '#0891b2', '#d55181'];
  var CATEGORICAL_OTHER = '#4b5470';

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

  function tableToggle(id) {
    return html`<button type="button" class="tf-link-btn" data-tf-table-toggle aria-controls="${id}-table" aria-expanded="false">${icon('list', 14)}<span data-tf-table-label>Show as a table</span></button>`;
  }

  function toggleChartTable(button) {
    var figure = closestAttr(button, 'data-tf-figure');
    if (!figure) return;
    var open = figure.getAttribute('data-table') !== 'open';
    figure.setAttribute('data-table', open ? 'open' : 'closed');
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    var label = one(button, '[data-tf-table-label]');
    if (label) label.textContent = open ? 'Hide the table' : 'Show as a table';
  }

  // Days as stacked columns. `series` is [{day, <key>: count, ...}], `keys` is
  // the stack from the baseline up: [{key, label, color}]. `href(row, key)` is
  // where a segment leads.
  function stackedBars(series, options) {
    options = options || {};
    var keys = options.keys || [];
    var width = Math.max(260, Math.round(options.width || 640));
    var height = options.height || 200;
    var pad = { top: 20, right: 8, bottom: 24, left: 36 };
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
    var barW = Math.max(3, Math.min(24, slot * 0.6));
    var id = chartId('tf-stack');
    var y = function (v) { return pad.top + plotH - (v / top) * plotH; };

    var grid = [];
    for (var t = 0; t <= top; t += step) {
      grid.push(html`<line x1="${pad.left}" x2="${width - pad.right}" y1="${round1(y(t))}" y2="${round1(y(t))}" stroke="${t === 0 ? COLORS.baseline : COLORS.grid}" stroke-width="1" shape-rendering="crispEdges"/>
        <text x="${pad.left - 8}" y="${round1(y(t) + 3.5)}" text-anchor="end" class="tf-axis">${num(t)}</text>`);
    }

    // Direct labels, sparingly: the newest day's total, and the busiest day's
    // when that is another day.
    var labelled = {};
    for (var li = totals.length - 1; li >= 0; li--) { if (totals[li] > 0) { labelled[li] = true; break; } }
    var peak = totals.indexOf(max);
    if (max > 0 && peak >= 0) labelled[peak] = true;

    var every = Math.max(1, Math.ceil(rows.length / Math.max(1, Math.floor(plotW / 46))));
    var bars = rows.map(function (row, i) {
      var x = round1(pad.left + i * slot + (slot - barW) / 2);
      var hitX = round1(pad.left + i * slot + 1);
      var hitW = round1(Math.max(1, slot - 2));
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
        var hit = html`<rect class="tf-hit" x="${hitX}" y="${round1(yTop)}" width="${hitW}" height="${round1(Math.max(h, 6))}"/>`;
        var label = shortDay(row.day) + ': ' + num(value) + ' ' + k.label.toLowerCase();
        var tip = shortDay(row.day) + ' · ' + k.label;
        var href = options.href ? options.href(row, k.key) : null;
        drawn.push(href
          ? html`<a href="${href}" aria-label="${label}, open these calls" data-tf-tip="${tip}" data-tf-tip-value="${num(value)}" data-tf-tip-color="${k.color}" data-tf-tip-action="Open these calls">${hit}${shape}</a>`
          : html`<g tabindex="0" aria-label="${label}" data-tf-tip="${tip}" data-tf-tip-value="${num(value)}" data-tf-tip-color="${k.color}">${hit}${shape}</g>`);
        base -= h;
      });
      var cap = labelled[i] && totals[i] > 0
        ? html`<text x="${round1(pad.left + i * slot + slot / 2)}" y="${round1(y(totals[i]) - 6)}" text-anchor="middle" class="tf-chart-label" aria-hidden="true">${num(totals[i])}</text>`
        : '';
      // Counted back from the newest day, so today always carries its label.
      var tick = (rows.length - 1 - i) % every === 0
        ? html`<text x="${round1(pad.left + i * slot + slot / 2)}" y="${height - 6}" text-anchor="middle" class="tf-axis">${shortDay(row.day)}</text>`
        : '';
      return html`<g>${drawn}${cap}${tick}</g>`;
    });

    var table = html`<table class="sr-only"><caption>${options.title || 'Calls per day'}</caption>
      <thead><tr><th scope="col">Day</th>${keys.map(function (k) { return html`<th scope="col">${k.label}</th>`; })}</tr></thead>
      <tbody>${rows.map(function (row) {
        return html`<tr><th scope="row">${shortDay(row.day)}</th>${keys.map(function (k) { return html`<td>${num(row[k.key])}</td>`; })}</tr>`;
      })}</tbody></table>`;

    return html`<figure class="tf-figure" data-tf-figure data-table="closed">
      <svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-labelledby="${id}-t ${id}-d" class="tf-svg">
        <title id="${id}-t">${options.title || 'Calls per day'}</title>
        <desc id="${id}-d">${options.desc || ''}</desc>
        ${grid}${bars}
      </svg>
      <div class="tf-chart-foot">${legend(keys)}${tableToggle(id)}</div>
      <div class="tf-chart-table" id="${id}-table">${table}</div>
    </figure>`;
  }

  function legend(keys) {
    if (!keys || keys.length < 2) return '';
    return html`<ul class="tf-legend" aria-hidden="true">${keys.map(function (k) {
      return html`<li><span class="tf-swatch${k.kind === 'line' ? ' tf-swatch-line' : ''}" style="background:${k.color}"></span>${k.label}</li>`;
    })}</ul>`;
  }

  // One row per thing, each a bar of one or more segments on a shared scale.
  // rows: [{label, href, sublabel, value, segments: [{value, label, color, href}]}]
  // The bars are HTML, so the 4px round data end holds at any width.
  function hbars(rows, options) {
    options = options || {};
    var totals = rows.map(function (r) {
      return r.segments.reduce(function (s, seg) { return s + (isNumber(seg.value) ? seg.value : 0); }, 0);
    });
    var max = options.max || Math.max.apply(null, [1].concat(totals));
    var keys = options.keys || [];
    return html`<figure class="tf-figure">
      <ul class="tf-hbars" aria-label="${options.title || ''}">
        ${rows.map(function (r, i) {
          var segs = r.segments.filter(function (seg) { return isNumber(seg.value) && seg.value > 0; });
          var described = r.segments.map(function (seg) { return num(seg.value) + ' ' + seg.label; }).join(', ');
          var width = Math.max(0, Math.min(100, (totals[i] / max) * 100));
          return html`<li>
            <div class="tf-hbar-head">
              ${r.href ? html`<a href="${r.href}" class="tf-link-quiet">${r.label}</a>` : html`<span>${r.label}</span>`}
              <span class="tf-hbar-value">${r.value !== undefined ? r.value : num(totals[i])}</span>
            </div>
            <div class="tf-hbar" role="img" aria-label="${r.label}: ${described}">
              <div class="tf-hbar-fill" style="width:${round1(width)}%">
                ${segs.map(function (seg) {
                  var label = r.label + ': ' + num(seg.value) + ' ' + seg.label;
                  var style = 'flex-grow:' + seg.value + ';background:' + seg.color;
                  return seg.href
                    ? html`<a href="${seg.href}" class="tf-hbar-seg" style="${style}" aria-label="${label}, open these calls" data-tf-tip="${r.label + ' · ' + seg.label}" data-tf-tip-value="${num(seg.value)}" data-tf-tip-color="${seg.color}" data-tf-tip-action="Open these calls"></a>`
                    : html`<span class="tf-hbar-seg" style="${style}" tabindex="0" aria-label="${label}" data-tf-tip="${r.label + ' · ' + seg.label}" data-tf-tip-value="${num(seg.value)}" data-tf-tip-color="${seg.color}"></span>`;
                })}
              </div>
            </div>
            ${r.sublabel ? html`<p class="tf-hbar-sub">${r.sublabel}</p>` : ''}
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
      var share = pct(total ? p.value / total : 0);
      var label = p.label + ': ' + num(p.value) + ' (' + share + ')';
      return p.href
        ? html`<a href="${p.href}" aria-label="${label}, open these calls" data-tf-tip="${p.label + ' · ' + share}" data-tf-tip-value="${num(p.value)}" data-tf-tip-color="${p.color}" data-tf-tip-action="Open these calls">${arc}</a>`
        : html`<g tabindex="0" aria-label="${label}" data-tf-tip="${p.label + ' · ' + share}" data-tf-tip-value="${num(p.value)}" data-tf-tip-color="${p.color}">${arc}</g>`;
    });
    return html`<figure class="tf-figure tf-donut">
      <svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img" aria-labelledby="${id}-t" class="tf-svg">
        <title id="${id}-t">${options.title || 'Share'}: ${parts.map(function (p) { return p.label + ' ' + num(p.value); }).join(', ')}</title>
        <circle cx="${size / 2}" cy="${size / 2}" r="${round1(r)}" fill="none" stroke="${COLORS.track}" stroke-width="${thick}"/>
        ${arcs}
        <text x="${size / 2}" y="${size / 2 + 3}" text-anchor="middle" class="tf-donut-value">${options.center !== undefined ? options.center : num(total)}</text>
        <text x="${size / 2}" y="${size / 2 + 19}" text-anchor="middle" class="tf-axis">${options.sub || ''}</text>
      </svg>
      <ul class="tf-donut-legend">${parts.map(function (p) {
        var inner = html`<span class="tf-swatch" style="background:${p.color}"></span><span class="tf-donut-label">${p.label}</span><span class="tf-donut-num">${num(p.value)}</span>`;
        return html`<li>${p.href ? html`<a href="${p.href}" class="tf-link-row tf-donut-row">${inner}</a>` : html`<span class="tf-donut-row">${inner}</span>`}</li>`;
      })}</ul>
    </figure>`;
  }

  // A trend line for a tile. values: numbers, oldest first. options.labels
  // names each point for its tooltip (a day, usually).
  function sparkline(values, options) {
    options = options || {};
    var width = options.width || 96;
    var height = options.height || 28;
    var vals = (values || []).map(function (v) { return isNumber(v) ? v : 0; });
    if (vals.length < 2) return '';
    var labels = Array.isArray(options.labels) ? options.labels : [];
    var max = Math.max.apply(null, [1].concat(vals));
    var step = (width - 8) / (vals.length - 1);
    var pts = vals.map(function (v, i) { return [round1(4 + i * step), round1(height - 4 - (v / max) * (height - 8))]; });
    var points = pts.map(function (p) { return p[0] + ',' + p[1]; });
    var last = pts[pts.length - 1];
    var color = options.color || COLORS.accent;
    return html`<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="${options.label || 'Trend'}: ${vals.join(', ')}" class="tf-svg tf-spark">
      <polyline points="${points.join(' ')} ${last[0]},${height - 4} 4,${height - 4}" fill="${color}" fill-opacity="0.1" stroke="none"/>
      <polyline points="${points.join(' ')}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="${last[0]}" cy="${last[1]}" r="4" fill="${color}" stroke="${COLORS.surface}" stroke-width="2"/>
      ${pts.map(function (p, i) {
        return html`<circle class="tf-hit" cx="${p[0]}" cy="${p[1]}" r="${Math.max(4, Math.min(8, step / 2))}" data-tf-tip="${labels[i] !== undefined ? labels[i] : (options.label || '')}" data-tf-tip-value="${num(vals[i])}" data-tf-tip-color="${color}"/>`;
      })}
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
    LOCAL_NAMES_STORAGE: LOCAL_NAMES_STORAGE,
    LOCAL_NAMES_HIDDEN_STORAGE: LOCAL_NAMES_HIDDEN_STORAGE,
    LOCAL_NAME_MAX: LOCAL_NAME_MAX,
    LOCAL_NAMES_MAX: LOCAL_NAMES_MAX,
    COLORS: COLORS,
    CATEGORICAL: CATEGORICAL,
    CATEGORICAL_OTHER: CATEGORICAL_OTHER,
    ICONS: ICONS,
    NAV: NAV,
    escapeHtml: escapeHtml,
    html: html,
    raw: raw,
    setHtml: setHtml,
    SafeHtml: SafeHtml,
    icon: icon,
    drawIcons: drawIcons,
    readSession: readSession,
    writeSession: writeSession,
    clearSession: clearSession,
    storedOperatorKey: storedOperatorKey,
    forgetOperatorKey: forgetOperatorKey,
    authHeaders: authHeaders,
    credentialInUse: credentialInUse,
    readLocalNames: readLocalNames,
    writeLocalNames: writeLocalNames,
    clearLocalNames: clearLocalNames,
    localNamesHidden: localNamesHidden,
    setLocalNamesHidden: setLocalNamesHidden,
    onLocalNamesChange: onLocalNamesChange,
    projectLabel: projectLabel,
    projectName: projectName,
    projectNameText: projectNameText,
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
    linkPages: linkPages,
    newSessionId: newSessionId,
    copyText: copyText,
    commandBlock: commandBlock,
    announce: announce,
    statusChip: statusChip,
    kbd: kbd,
    emptyState: emptyState,
    skeleton: skeleton,
    statTile: statTile,
    delta: delta,
    setBusy: setBusy,
    toast: toast,
    dismissToast: dismissToast,
    openDialog: openDialog,
    closeDialog: closeDialog,
    selectTab: selectTab,
    reducedMotion: reducedMotion,
    countUp: countUp,
    animateNumbers: animateNumbers,
    reveal: reveal,
    pulse: pulse,
    applyTailwindTheme: applyTailwindTheme,
    mountNav: mountNav,
    palette: {
      open: openPalette,
      close: closePalette,
      toggle: togglePalette,
      isOpen: function () { return palette.open; },
      query: setPaletteQuery,
      move: movePalette,
      choose: choosePalette,
      results: function () {
        return palette.results.map(function (item) {
          return { group: item.group, id: item.id, text: item.text, href: item.href || null, action: !!item.run };
        });
      },
      active: function () { return palette.active; },
      score: fuzzyScore
    },
    stackedBars: stackedBars,
    hbars: hbars,
    donut: donut,
    sparkline: sparkline,
    legend: legend
  };
})(typeof window !== 'undefined' ? window : this);
