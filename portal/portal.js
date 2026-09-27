// Public OmniBioAI video portal.
//
// Read-only by construction: the only network requests are GET /videos.json
// and GET /videos/<file>. The catalog is generated at build time and contains
// only entries explicitly classified PUBLIC (see scripts/build_public.py), so
// there is deliberately no directory-listing fallback here: if the catalog is
// missing or malformed the page shows an empty state rather than guessing at
// what might be served.
//
// All catalog text is written with textContent / DOM APIs, never by parsing markup strings.
(function () {
  'use strict';

  var TAGS = ['intro', 'tutorial', 'workflow', 'demo', 'hpc', 'documentation'];
  var FILENAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(mp4|webm)$/;
  var THUMBNAIL_RE = /^(?:\/[A-Za-z0-9][A-Za-z0-9._\/-]{0,180}|[A-Za-z0-9][A-Za-z0-9._\/-]{0,180})\.(jpg|jpeg|png|webp)$/;

  var allVideos = [];
  var currentFilter = 'all';
  var categories = [];
  var suggestionIndex = [];
  var suggestions = [];
  var activeSuggestion = -1;
  var returnFocus = null;
  var search = document.getElementById('searchInput');
  var listbox = document.getElementById('suggestions');
  var categoryOrder = ['Getting Started', 'Platform', 'Workflows', 'AI & RAG',
    'Bioinformatics', 'Security', 'Administration', 'Documentation'].concat(TAGS.map(labelTag));
  var previews = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      var video = entry.target;
      video.src = video.dataset.preview;
      previews.unobserve(video);
    });
  }, { rootMargin: '160px' });

  // Normalize once per catalog field; punctuation/spacing variants share a key.
  function normaliseText(value) {
    return value.normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase()
      .replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
  }

  function compact(value) { return normaliseText(value).replace(/ /g, ''); }
  function categoryId(value) { return normaliseText(value).replace(/ /g, '-'); }
  function words(value) {
    return Array.isArray(value) ? value.filter(function (s) {
      return typeof s === 'string' && s.trim().length > 0 && s.trim().length <= 120;
    }).slice(0, 32).map(function (s) { return s.trim(); }) : [];
  }

  function queryTokens(query) { return normaliseText(query).split(' ').filter(Boolean); }

  function matches(index, tokens) {
    return tokens.every(function (token) { return index.indexOf(token) >= 0; });
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function stateBox(icon, heading, message) {
    var box = el('div', 'state-box');
    box.appendChild(el('div', 'state-icon', icon));
    box.appendChild(el('h3', null, heading));
    box.appendChild(el('p', null, message));
    return box;
  }

  function setContent(node) {
    var root = document.getElementById('content');
    root.replaceChildren(node);
  }

  function labelTag(tag) {
    return tag === 'hpc' ? 'HPC' : tag.charAt(0).toUpperCase() + tag.slice(1);
  }

  function formatDuration(value) {
    if (typeof value === 'number' && isFinite(value) && value > 0) {
      var total = Math.round(value);
      var hours = Math.floor(total / 3600);
      var minutes = Math.floor((total % 3600) / 60);
      var seconds = total % 60;
      if (hours) return hours + ':' + String(minutes).padStart(2, '0') + ':' + String(seconds).padStart(2, '0');
      return minutes + ':' + String(seconds).padStart(2, '0');
    }
    if (typeof value === 'string' && value.trim()) return value.trim();
    return '';
  }

  function normalise(raw) {
    if (!Array.isArray(raw)) return [];
    var out = [];
    raw.forEach(function (v) {
      if (!v || typeof v !== 'object') return;
      if ('visibility' in v && v.visibility !== 'PUBLIC') return;
      if (typeof v.filename !== 'string' || !FILENAME_RE.test(v.filename)) return;
      var tag = TAGS.indexOf(v.tag) >= 0 ? v.tag : 'tutorial';
      var category = typeof v.category === 'string' && v.category.trim().length <= 80 &&
        categoryId(v.category) && categoryId(v.category) !== 'all' ? v.category.trim() : labelTag(tag);
      var known = categoryOrder.find(function (name) { return categoryId(name) === categoryId(category); });
      category = known || category;
      var terms = ['tags', 'keywords', 'services', 'modules', 'workflows'].flatMap(function (key) { return words(v[key]); });
      var video = {
        filename: v.filename,
        title: typeof v.title === 'string' ? v.title : v.filename,
        desc: typeof v.desc === 'string' ? v.desc : '',
        tag: tag,
        category: category,
        categoryId: categoryId(category),
        terms: terms,
        featured: v.featured === true,
        order: typeof v.order === 'number' && isFinite(v.order) ? v.order : 999,
        thumbnail: typeof v.thumbnail === 'string' && THUMBNAIL_RE.test(v.thumbnail) ? v.thumbnail : '',
        duration: formatDuration(v.duration),
        url: '/videos/' + encodeURIComponent(v.filename)
      };
      video.index = [video.title, video.desc, category, tag].concat(terms).map(compact).join(' ');
      out.push(video);
    });
    out.sort(function (a, b) { return a.order - b.order || a.filename.localeCompare(b.filename); });
    return out;
  }

  function card(v) {
    var c = el('article', 'video-card');
    c.tabIndex = 0;
    c.setAttribute('role', 'button');
    c.setAttribute('aria-label', 'Play ' + v.title);

    var thumb = el('div', 'thumb');
    var fallback = el('div', 'thumb-fallback');
    fallback.appendChild(el('span', 'fallback-mark', 'OB'));
    thumb.appendChild(fallback);
    if (v.thumbnail) {
      var img = document.createElement('img');
      img.src = v.thumbnail.charAt(0) === '/' ? v.thumbnail : '/' + v.thumbnail;
      img.alt = '';
      img.loading = 'lazy';
      img.addEventListener('error', function () { img.remove(); thumb.classList.add('missing-thumb'); });
      thumb.appendChild(img);
    } else {
      thumb.classList.add('missing-thumb');
      var vid = document.createElement('video');
      vid.dataset.preview = v.url + '#t=2';
      vid.preload = 'metadata';
      vid.muted = true;
      thumb.appendChild(vid);
      previews.observe(vid);
    }

    var overlay = el('div', 'thumb-overlay');
    var circle = el('div', 'play-circle', '▶');
    overlay.appendChild(circle);
    thumb.appendChild(overlay);
    thumb.appendChild(el('span', 'thumb-tag tag-' + v.tag, labelTag(v.tag)));
    if (v.duration) thumb.appendChild(el('span', 'thumb-duration', v.duration));
    c.appendChild(thumb);

    var body = el('div', 'card-body');
    body.appendChild(el('div', 'card-title', v.title));
    if (v.desc) body.appendChild(el('div', 'card-desc', v.desc));
    var footer = el('div', 'card-footer');
    footer.appendChild(el('span', null, v.category));
    footer.appendChild(el('span', null, v.duration || 'Play'));
    body.appendChild(footer);
    c.appendChild(body);

    function open() { openModal(v); }
    c.addEventListener('click', open);
    c.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
    return c;
  }

  function section(title, videos) {
    var group = el('section', 'library-section');
    group.appendChild(el('h2', null, title));
    var grid = el('div', 'video-grid');
    videos.forEach(function (v) {
      // Reuse cards so typing never recreates media elements or reloads previews.
      if (!v.card) v.card = card(v);
      grid.appendChild(v.card);
    });
    group.appendChild(grid);
    return group;
  }

  function render(videos) {
    var n = videos.length;
    var query = search.value.trim();
    document.getElementById('videoCount').textContent = n + ' video' + (n !== 1 ? 's' : '');
    document.getElementById('resultStatus').textContent = query ?
      n + ' result' + (n !== 1 ? 's' : '') + ' for “' + query + '”' : '';
    document.getElementById('clearSearch').hidden = !search.value;
    if (n === 0) {
      setContent(stateBox('⌕', 'No videos found' + (query ? ' for “' + query + '”' : ''),
        'Try another keyword or category.'));
      return;
    }
    var root = document.createDocumentFragment();
    if (query || currentFilter !== 'all') {
      root.appendChild(section(query ? 'Search Results' : videos[0].category, videos));
    } else {
      var featured = videos.filter(function (v) { return v.featured; });
      if (featured.length) root.appendChild(section('Featured', featured));
      categories.forEach(function (category) {
        var entries = videos.filter(function (v) { return v.categoryId === category.id && !v.featured; });
        if (entries.length) root.appendChild(section(category.label, entries));
      });
    }
    setContent(root);
  }

  function prepareDiscovery() {
    var seen = new Map();
    allVideos.forEach(function (v) {
      if (!seen.has(v.categoryId)) seen.set(v.categoryId, { id: v.categoryId, label: v.category });
    });
    categories = Array.from(seen.values()).sort(function (a, b) {
      var ai = categoryOrder.indexOf(a.label), bi = categoryOrder.indexOf(b.label);
      return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi) || a.label.localeCompare(b.label);
    });
    var filters = document.getElementById('categoryFilters');
    [{ id: 'all', label: 'All' }].concat(categories).forEach(function (category) {
      var button = el('button', 'filter-btn', category.label);
      button.type = 'button';
      button.dataset.tag = category.id;
      button.addEventListener('click', function () {
        currentFilter = category.id;
        closeSuggestions();
        applyFilters();
      });
      filters.appendChild(button);
    });
    // Category membership keeps autocomplete within the selected category.
    var terms = new Map();
    allVideos.forEach(function (v) {
      suggestionIndex.push({ kind: 'Video', text: v.title, index: v.index, categories: [v.categoryId], video: v });
      v.terms.concat(v.tag).forEach(function (term) {
        var key = compact(term);
        if (!key) return;
        if (!terms.has(key)) terms.set(key, { kind: 'Keyword', text: term, index: key, categories: [] });
        terms.get(key).categories.push(v.categoryId);
      });
    });
    suggestionIndex = suggestionIndex.concat(Array.from(terms.values()), categories.map(function (c) {
      return { kind: 'Category', text: c.label, index: compact(c.label), categories: [c.id], category: c.id };
    }));
  }

  function closeSuggestions() {
    suggestions = [];
    activeSuggestion = -1;
    listbox.replaceChildren();
    listbox.hidden = true;
    search.setAttribute('aria-expanded', 'false');
    search.removeAttribute('aria-activedescendant');
  }

  function showSuggestions() {
    closeSuggestions();
    var tokens = queryTokens(search.value);
    if (!tokens.length) return;
    var seen = new Set();
    var limits = { Video: 4, Keyword: 3, Category: 2 };
    suggestionIndex.forEach(function (suggestion) {
      if (suggestions.length >= 8 || !limits[suggestion.kind]) return;
      if (currentFilter !== 'all' && suggestion.categories.indexOf(currentFilter) < 0) return;
      if (!matches(suggestion.index, tokens)) return;
      var key = compact(suggestion.text);
      if (seen.has(key)) return;
      seen.add(key);
      limits[suggestion.kind] -= 1;
      suggestions.push(suggestion);
    });
    suggestions.forEach(function (suggestion, i) {
      var option = el('div', 'suggestion');
      option.id = 'suggestion-' + i;
      option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', 'false');
      option.appendChild(el('span', 'suggestion-kind', suggestion.kind));
      option.appendChild(el('span', 'suggestion-text', suggestion.text));
      option.addEventListener('mousedown', function (event) { event.preventDefault(); });
      option.addEventListener('click', function () { selectSuggestion(suggestion); });
      listbox.appendChild(option);
    });
    listbox.hidden = suggestions.length === 0;
    search.setAttribute('aria-expanded', String(suggestions.length > 0));
  }

  function selectSuggestion(suggestion) {
    closeSuggestions();
    if (suggestion.video) {
      openModal(suggestion.video);
      return;
    }
    if (suggestion.category) {
      currentFilter = suggestion.category;
      search.value = '';
    } else {
      search.value = suggestion.text;
    }
    applyFilters();
    search.focus();
  }

  function readUrl() {
    var params = new URLSearchParams(location.search);
    search.value = params.get('q') || '';
    var category = params.get('category');
    currentFilter = categories.some(function (c) { return c.id === category; }) ? category : 'all';
  }

  // Nothing is published yet: say so plainly instead of implying a search/filter failure.
  function showComingSoon() {
    document.getElementById('statusPill').textContent = '● COMING SOON';
    document.getElementById('videoCount').textContent = '';
    document.getElementById('controls').hidden = true;
    setContent(stateBox('🎬', 'Videos coming soon', 'Tutorials and platform walkthroughs are on the way. Please check back soon.'));
  }

  function applyFilters() {
    var tokens = queryTokens(search.value);
    document.querySelectorAll('.filter-btn').forEach(function (button) {
      var selected = button.dataset.tag === currentFilter;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    render(allVideos.filter(function (v) {
      return (currentFilter === 'all' || v.categoryId === currentFilter) && matches(v.index, tokens);
    }));
    var params = new URLSearchParams();
    if (search.value.trim()) params.set('q', search.value.trim());
    if (currentFilter !== 'all') params.set('category', currentFilter);
    var query = params.toString();
    history.replaceState(null, '', location.pathname + (query ? '?' + query : '') + location.hash);
  }

  function openModal(v) {
    closeSuggestions();
    returnFocus = document.activeElement;
    document.getElementById('modalVideo').src = v.url;
    document.getElementById('modalTitle').textContent = v.title;
    document.getElementById('modalMeta').textContent = v.category + (v.duration ? ' · ' + v.duration : '') + ' · OmniBioAI Platform';
    document.getElementById('modal').classList.add('open');
    document.body.classList.add('modal-open');
    document.getElementById('modalClose').focus();
  }

  function closeModal() {
    if (!document.getElementById('modal').classList.contains('open')) return;
    var video = document.getElementById('modalVideo');
    video.pause();
    video.removeAttribute('src');
    video.load();
    document.getElementById('modal').classList.remove('open');
    document.body.classList.remove('modal-open');
    if (returnFocus && returnFocus.isConnected) returnFocus.focus();
  }

  function load() {
    fetch('/videos.json', { credentials: 'omit', cache: 'no-cache' })
      .then(function (res) {
        if (!res.ok) throw new Error('catalog unavailable');
        return res.json();
      })
      .then(function (raw) {
        allVideos = normalise(raw);
        if (allVideos.length === 0) {
          showComingSoon();
          return;
        }
        document.getElementById('statusPill').textContent = '● ' + allVideos.length + ' VIDEOS';
        prepareDiscovery();
        readUrl();
        applyFilters();
      })
      .catch(function () {
        allVideos = [];
        document.getElementById('controls').hidden = true;
        document.getElementById('statusPill').textContent = '● UNAVAILABLE';
        document.getElementById('videoCount').textContent = '';
        setContent(stateBox('📂', 'Videos unavailable', 'The video catalog could not be loaded. Please try again later.'));
      });
  }

  search.addEventListener('input', function () { applyFilters(); showSuggestions(); });
  search.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') { closeSuggestions(); event.stopPropagation(); }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (listbox.hidden) showSuggestions();
      if (!suggestions.length) return;
      activeSuggestion = (activeSuggestion + (event.key === 'ArrowDown' ? 1 :
        (activeSuggestion < 0 ? 0 : -1)) + suggestions.length) % suggestions.length;
      Array.from(listbox.children).forEach(function (option, i) {
        option.setAttribute('aria-selected', String(i === activeSuggestion));
      });
      var active = listbox.children[activeSuggestion];
      search.setAttribute('aria-activedescendant', active.id);
      active.scrollIntoView({ block: 'nearest' });
    }
    if (event.key === 'Enter' && !listbox.hidden && suggestions.length) {
      event.preventDefault();
      selectSuggestion(suggestions[activeSuggestion < 0 ? 0 : activeSuggestion]);
    }
    if (event.key === 'Tab') closeSuggestions();
  });
  document.getElementById('clearSearch').addEventListener('click', function () {
    search.value = '';
    closeSuggestions();
    applyFilters();
    search.focus();
  });
  document.addEventListener('pointerdown', function (event) {
    if (!document.querySelector('.search-wrap').contains(event.target)) closeSuggestions();
  });
  window.addEventListener('popstate', function () { readUrl(); closeSuggestions(); applyFilters(); });
  document.getElementById('modalClose').addEventListener('click', closeModal);
  document.getElementById('modal').addEventListener('click', function (e) {
    if (e.target === e.currentTarget) closeModal();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeModal();
    if (e.key === 'Tab' && document.getElementById('modal').classList.contains('open')) {
      var first = document.getElementById('modalVideo');
      var last = document.getElementById('modalClose');
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  load();
})();
