(function (scope) {
  'use strict';

  const EMBED_READY_EVENT = 'omnibioai.embed.ready.v1';
  const EMBED_THEME_EVENT = 'omnibioai.theme.set.v1';
  const PRODUCTION_STUDIO_ORIGIN = 'https://webstudio.omnibioai.org';
  const MESSAGE_KEYS = ['accent', 'density', 'motion', 'theme', 'type'];
  const ALLOWED = Object.freeze({
    theme: new Set(['light', 'dark']),
    accent: new Set(['teal', 'blue', 'purple', 'orange']),
    density: new Set(['comfortable', 'compact']),
    motion: new Set(['system', 'reduce']),
  });

  function configuredStudioOrigin(value) {
    if (typeof value !== 'string' || !value) return null;
    try {
      const parsed = new URL(value);
      if (!/^https?:$/.test(parsed.protocol) || parsed.origin !== value || parsed.pathname !== '/' ||
          parsed.search || parsed.hash || parsed.username || parsed.password) return null;
      return parsed.origin;
    } catch {
      return null;
    }
  }

  function parseThemeMessage(data) {
    if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
    const keys = Object.keys(data).sort();
    if (keys.length !== MESSAGE_KEYS.length || keys.some((key, index) => key !== MESSAGE_KEYS[index])) return null;
    if (data.type !== EMBED_THEME_EVENT || !Object.entries(ALLOWED).every(([key, values]) => values.has(data[key]))) return null;
    return { theme: data.theme, accent: data.accent, density: data.density, motion: data.motion };
  }

  function applyAppearance(root, appearance) {
    root.dataset.theme = appearance.theme;
    root.dataset.accent = appearance.accent;
    root.dataset.density = appearance.density;
    root.dataset.motion = appearance.motion;
  }

  function connectAppearanceMessages(win, doc, devStudioOrigin) {
    if (win.parent === win || !doc.referrer) return function () {};
    let parentOrigin;
    try {
      parentOrigin = new URL(doc.referrer).origin;
    } catch {
      return function () {};
    }

    const allowedOrigins = new Set([PRODUCTION_STUDIO_ORIGIN]);
    const validatedDevOrigin = configuredStudioOrigin(devStudioOrigin);
    if (validatedDevOrigin) allowedOrigins.add(validatedDevOrigin);
    if (!allowedOrigins.has(parentOrigin)) return function () {};

    const onMessage = function (event) {
      if (event.origin !== parentOrigin || event.source !== win.parent) return;
      const appearance = parseThemeMessage(event.data);
      if (appearance) applyAppearance(doc.documentElement, appearance);
    };
    win.addEventListener('message', onMessage);
    win.parent.postMessage({ type: EMBED_READY_EVENT }, parentOrigin);
    return function () { win.removeEventListener('message', onMessage); };
  }

  const api = Object.freeze({
    EMBED_READY_EVENT,
    EMBED_THEME_EVENT,
    configuredStudioOrigin,
    parseThemeMessage,
    applyAppearance,
    connectAppearanceMessages,
  });
  scope.OmniBioAIEmbedAppearance = api;

  const meta = scope.document.querySelector('meta[name="omnibioai-studio-dev-origin"]');
  connectAppearanceMessages(scope, scope.document, meta ? meta.content : '');
})(window);
