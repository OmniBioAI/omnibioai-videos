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

  var TAGS = ['intro', 'tutorial', 'workflow', 'demo', 'hpc'];
  var FILENAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(mp4|webm)$/;

  var allVideos = [];
  var currentFilter = 'all';

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

  function normalise(raw) {
    if (!Array.isArray(raw)) return [];
    var out = [];
    raw.forEach(function (v) {
      if (!v || typeof v !== 'object') return;
      if (typeof v.filename !== 'string' || !FILENAME_RE.test(v.filename)) return;
      out.push({
        filename: v.filename,
        title: typeof v.title === 'string' ? v.title : v.filename,
        desc: typeof v.desc === 'string' ? v.desc : '',
        tag: TAGS.indexOf(v.tag) >= 0 ? v.tag : 'tutorial',
        order: typeof v.order === 'number' ? v.order : 999,
        url: '/videos/' + encodeURIComponent(v.filename)
      });
    });
    out.sort(function (a, b) { return a.order - b.order; });
    return out;
  }

  function card(v) {
    var c = el('div', 'video-card');
    c.tabIndex = 0;
    c.setAttribute('role', 'button');

    var thumb = el('div', 'thumb');
    var vid = document.createElement('video');
    vid.src = v.url + '#t=2';
    vid.preload = 'metadata';
    vid.muted = true;
    thumb.appendChild(vid);

    var overlay = el('div', 'thumb-overlay');
    var circle = el('div', 'play-circle', '▶');
    overlay.appendChild(circle);
    thumb.appendChild(overlay);
    thumb.appendChild(el('span', 'thumb-tag tag-' + v.tag, v.tag));
    if (v.order !== 999) thumb.appendChild(el('span', 'thumb-order', '#' + v.order));
    c.appendChild(thumb);

    var body = el('div', 'card-body');
    body.appendChild(el('div', 'card-title', v.title));
    body.appendChild(el('div', 'card-desc', v.desc));
    var footer = el('div', 'card-footer');
    footer.appendChild(el('span', null, 'OmniBioAI Platform'));
    footer.appendChild(el('span', null, '▶ Play'));
    body.appendChild(footer);
    c.appendChild(body);

    function open() { openModal(v); }
    c.addEventListener('click', open);
    c.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
    return c;
  }

  function render(videos) {
    var n = videos.length;
    document.getElementById('videoCount').textContent = n + ' video' + (n !== 1 ? 's' : '');
    if (n === 0) {
      // Only reachable when the catalog has videos but the search/filter matches none.
      setContent(stateBox('🔎', 'No videos found', 'Try a different search term or filter.'));
      return;
    }
    var grid = el('div', 'video-grid');
    videos.forEach(function (v) { grid.appendChild(card(v)); });
    setContent(grid);
  }

  // Nothing is published yet: say so plainly instead of implying a search/filter failure.
  function showComingSoon() {
    document.getElementById('statusPill').textContent = '● COMING SOON';
    document.getElementById('videoCount').textContent = '';
    document.getElementById('controls').hidden = true;
    setContent(stateBox('🎬', 'Videos coming soon', 'Tutorials and platform walkthroughs are on the way. Please check back soon.'));
  }

  function applyFilters() {
    var q = document.getElementById('searchInput').value.toLowerCase();
    render(allVideos.filter(function (v) {
      var tagOk = currentFilter === 'all' || v.tag === currentFilter;
      var qOk = !q || v.title.toLowerCase().indexOf(q) >= 0 || v.desc.toLowerCase().indexOf(q) >= 0;
      return tagOk && qOk;
    }));
  }

  function openModal(v) {
    document.getElementById('modalVideo').src = v.url;
    document.getElementById('modalTitle').textContent = v.title;
    document.getElementById('modalMeta').textContent = v.tag + ' · OmniBioAI Platform';
    document.getElementById('modal').classList.add('open');
    document.body.classList.add('modal-open');
  }

  function closeModal() {
    var video = document.getElementById('modalVideo');
    video.pause();
    video.removeAttribute('src');
    video.load();
    document.getElementById('modal').classList.remove('open');
    document.body.classList.remove('modal-open');
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
        render(allVideos);
      })
      .catch(function () {
        allVideos = [];
        document.getElementById('statusPill').textContent = '● UNAVAILABLE';
        document.getElementById('videoCount').textContent = '';
        setContent(stateBox('📂', 'Videos unavailable', 'The video catalog could not be loaded. Please try again later.'));
      });
  }

  document.getElementById('searchInput').addEventListener('input', applyFilters);
  document.querySelectorAll('.filter-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      currentFilter = btn.getAttribute('data-tag');
      document.querySelectorAll('.filter-btn').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      applyFilters();
    });
  });
  document.getElementById('modalClose').addEventListener('click', closeModal);
  document.getElementById('modal').addEventListener('click', function (e) {
    if (e.target === e.currentTarget) closeModal();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeModal();
  });

  load();
})();
