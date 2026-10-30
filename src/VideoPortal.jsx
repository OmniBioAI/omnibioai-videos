import React, { useEffect, useMemo, useRef, useState } from "react";

const TAGS = ["intro", "tutorial", "workflow", "demo", "hpc", "documentation"];
const FILENAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(mp4|webm)$/;
const THUMBNAIL_RE = /^(?:\/[A-Za-z0-9][A-Za-z0-9._/-]{0,180}|[A-Za-z0-9][A-Za-z0-9._/-]{0,180})\.(?:jpg|jpeg|png|webp)$/;
const CATEGORY_ORDER = [
  "Getting Started", "Platform", "Workflows", "AI & RAG", "Bioinformatics",
  "Security", "Administration", "Documentation",
];

function normaliseText(value) {
  return value.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

function compact(value) {
  return normaliseText(value).replaceAll(" ", "");
}

function categoryId(value) {
  return normaliseText(value).replaceAll(" ", "-");
}

function labelTag(tag) {
  return tag === "hpc" ? "HPC" : tag.charAt(0).toUpperCase() + tag.slice(1);
}

function words(value) {
  return Array.isArray(value)
    ? value.filter((item) => typeof item === "string" && item.trim() && item.trim().length <= 120)
      .slice(0, 32).map((item) => item.trim())
    : [];
}

function formatDuration(value) {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    const total = Math.round(value);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    return hours
      ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
      : `${minutes}:${String(seconds).padStart(2, "0")}`;
  }
  return typeof value === "string" ? value.trim() : "";
}

function safeThumbnail(value) {
  if (typeof value !== "string" || !THUMBNAIL_RE.test(value)) return "";
  const relative = value.replace(/^\/+/, "");
  return relative.split("/").some((part) => !part || part === "." || part === "..") ? "" : relative;
}

function normaliseCatalog(raw) {
  if (!Array.isArray(raw)) return [];
  const videos = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    if ("visibility" in item && item.visibility !== "PUBLIC") continue;
    if (typeof item.filename !== "string" || !FILENAME_RE.test(item.filename)) continue;
    const tag = TAGS.includes(item.tag) ? item.tag : "tutorial";
    let category = typeof item.category === "string" && item.category.trim().length <= 80
      ? item.category.trim() : labelTag(tag);
    if (!categoryId(category) || categoryId(category) === "all") category = labelTag(tag);
    category = CATEGORY_ORDER.find((known) => categoryId(known) === categoryId(category)) || category;
    const terms = ["tags", "keywords", "services", "modules", "workflows"].flatMap((key) => words(item[key]));
    const video = {
      filename: item.filename,
      title: typeof item.title === "string" ? item.title : item.filename,
      desc: typeof item.desc === "string" ? item.desc : "",
      tag,
      category,
      categoryId: categoryId(category),
      terms,
      featured: item.featured === true,
      order: typeof item.order === "number" && Number.isFinite(item.order) ? item.order : 999,
      thumbnail: safeThumbnail(item.thumbnail),
      duration: formatDuration(item.duration),
      url: `videos/${encodeURIComponent(item.filename)}`,
    };
    video.index = [video.title, video.desc, category, tag, ...terms].map(compact).join(" ");
    videos.push(video);
  }
  return videos.sort((a, b) => a.order - b.order || a.filename.localeCompare(b.filename));
}

function initialLocation() {
  const params = new URLSearchParams(window.location.search);
  return {
    query: params.get("q") || "",
    filter: params.get("category") || "all",
    video: params.get("v") || "",
  };
}

function metaLine(video) {
  return `${video.category}${video.duration ? ` · ${video.duration}` : ""} · OmniBioAI Platform`;
}

function Poster({ video, className }) {
  const videoRef = useRef(null);
  const [imageFailed, setImageFailed] = useState(false);
  useEffect(() => {
    const node = videoRef.current;
    if (!node || video.thumbnail || !("IntersectionObserver" in window)) return undefined;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) {
        node.src = `${video.url}#t=2`;
        observer.disconnect();
      }
    }, { rootMargin: "160px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, [video]);
  return (
    <div className={`${className}${!video.thumbnail || imageFailed ? " missing-thumb" : ""}`}>
      <div className="thumb-fallback" aria-hidden="true"><span className="fallback-mark">OB</span></div>
      {video.thumbnail && !imageFailed
        ? <img src={video.thumbnail} alt="" loading="lazy" onError={() => setImageFailed(true)} />
        : <video ref={videoRef} preload="metadata" muted tabIndex={-1} aria-hidden="true" />}
    </div>
  );
}

function VideoCard({ video, onOpen, compactCard = false }) {
  if (compactCard) {
    return (
      <button className="up-next-item" type="button" onClick={() => onOpen(video, true)}
        aria-label={`Play next: ${video.title}`}>
        <Poster video={video} className="up-next-thumb" />
        <span className="up-next-text"><span className="up-next-title">{video.title}</span><span className="up-next-meta">{metaLine(video)}</span></span>
      </button>
    );
  }
  return (
    <article className="video-card" tabIndex={0} role="button" aria-label={`Play ${video.title}`}
      onClick={(event) => onOpen(video, false, event.currentTarget)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onOpen(video, false, event.currentTarget);
        }
      }}>
      <Poster video={video} className="thumb" />
      <div className="thumb-overlay" aria-hidden="true"><div className="play-circle">▶</div></div>
      <span className={`thumb-tag tag-${video.tag}`}>{labelTag(video.tag)}</span>
      {video.duration && <span className="thumb-duration">{video.duration}</span>}
      <div className="card-body">
        <span className="channel-avatar" aria-hidden="true">OB</span>
        <div className="card-text">
          <div className="card-title">{video.title}</div>
          <div className="card-channel">OmniBioAI</div>
          {video.desc && <div className="card-desc">{video.desc}</div>}
          <div className="card-footer"><span>{video.category}</span><span>{labelTag(video.tag)}</span></div>
        </div>
      </div>
    </article>
  );
}

function Hero({ video, onOpen }) {
  return (
    <section className="hero" aria-labelledby="heroTitle">
      <div className="hero-media" onClick={() => onOpen(video)}>
        <Poster video={video} className="hero-poster" />
        <div className="thumb-overlay" aria-hidden="true"><div className="play-circle play-circle-lg">▶</div></div>
        {video.duration && <span className="hero-duration">{video.duration}</span>}
      </div>
      <div className="hero-info">
        <p className="hero-eyebrow">Featured</p>
        <h2 className="hero-title" id="heroTitle">{video.title}</h2>
        {video.desc && <p className="hero-desc">{video.desc}</p>}
        <p className="hero-meta">{metaLine(video)}</p>
        <button className="hero-watch" type="button" onClick={() => onOpen(video)}>
          <span className="hero-watch-icon" aria-hidden="true">▶</span>Watch now
        </button>
      </div>
    </section>
  );
}

function buildSuggestions(videos, query, filter) {
  if (!query.trim()) return [];
  const needle = compact(query);
  const scoped = filter === "all" ? videos : videos.filter((video) => video.categoryId === filter);
  const result = [];
  const seen = new Set();
  const add = (type, label, value, video) => {
    const key = `${type}:${compact(value)}`;
    if (!seen.has(key) && compact(value).includes(needle)) {
      seen.add(key);
      result.push({ type, label, value, video });
    }
  };
  for (const video of scoped) {
    add("Video", video.title, video.title, video);
    add("Category", video.category, video.category);
    for (const term of video.terms) add("Keyword", term, term);
  }
  return result.slice(0, 8);
}

export default function VideoPortal() {
  const initial = useMemo(initialLocation, []);
  const [videos, setVideos] = useState([]);
  const [loadState, setLoadState] = useState("loading");
  const [query, setQuery] = useState(initial.query);
  const [filter, setFilter] = useState(initial.filter);
  const [watching, setWatching] = useState(initial.video);
  const [suggestionsOpen, setSuggestionsOpen] = useState(false);
  const [activeSuggestion, setActiveSuggestion] = useState(-1);
  const [playerFailed, setPlayerFailed] = useState(false);
  const searchRef = useRef(null);
  const titleRef = useRef(null);
  const returnFocus = useRef(null);
  const studioConfig = document.querySelector("[data-studio-link]");
  const studioHref = studioConfig?.getAttribute("href") || "";

  const categories = useMemo(() => {
    const found = [...new Map(videos.map((video) => [video.categoryId, video.category])).entries()];
    return found.sort((a, b) => {
      const ai = CATEGORY_ORDER.indexOf(a[1]);
      const bi = CATEGORY_ORDER.indexOf(b[1]);
      return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi) || a[1].localeCompare(b[1]);
    });
  }, [videos]);

  useEffect(() => {
    let active = true;
    fetch("videos.json", { credentials: "omit" })
      .then((response) => {
        if (!response.ok) throw new Error("catalog unavailable");
        return response.json();
      })
      .then((catalog) => {
        if (!active) return;
        const nextVideos = normaliseCatalog(catalog);
        setVideos(nextVideos);
        setLoadState(nextVideos.length ? "ready" : "empty");
      })
      .catch(() => active && setLoadState("error"));
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (loadState !== "ready") return;
    if (filter !== "all" && !categories.some(([id]) => id === filter)) setFilter("all");
    if (watching && !videos.some((video) => video.filename === watching)) setWatching("");
  }, [categories, filter, loadState, videos, watching]);

  useEffect(() => {
    const onPop = () => {
      const next = initialLocation();
      setQuery(next.query);
      setFilter(next.filter);
      setWatching(next.video);
      setSuggestionsOpen(false);
      queueMicrotask(() => returnFocus.current?.focus());
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    const onKey = (event) => {
      if (event.key !== "Escape") return;
      if (watching) {
        closeWatch();
      } else if (suggestionsOpen) {
        setSuggestionsOpen(false);
        setActiveSuggestion(-1);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  const selected = videos.find((video) => video.filename === watching);
  useEffect(() => {
    document.title = selected ? `${selected.title} · OmniBioAI Videos` : "OmniBioAI · Video Tutorials";
    setPlayerFailed(false);
    if (selected) queueMicrotask(() => titleRef.current?.focus());
  }, [selected]);

  const filtered = useMemo(() => {
    const tokens = normaliseText(query).split(" ").filter(Boolean);
    return videos.filter((video) => (filter === "all" || video.categoryId === filter)
      && tokens.every((token) => video.index.includes(token.replaceAll(" ", ""))));
  }, [filter, query, videos]);
  const suggestions = useMemo(() => buildSuggestions(videos, query, filter), [filter, query, videos]);
  const featured = videos.find((video) => video.featured) || videos[0];
  const atHome = !query && filter === "all" && !selected;

  function writeUrl({ nextQuery = query, nextFilter = filter, nextVideo = watching, mode = "replace" } = {}) {
    const params = new URLSearchParams();
    if (nextQuery) params.set("q", nextQuery);
    if (nextFilter !== "all") params.set("category", nextFilter);
    if (nextVideo) params.set("v", nextVideo);
    const url = `${window.location.pathname}${params.size ? `?${params}` : ""}`;
    window.history[`${mode}State`](null, "", url);
  }

  function changeQuery(value) {
    setQuery(value);
    setWatching("");
    setSuggestionsOpen(Boolean(value));
    setActiveSuggestion(-1);
    writeUrl({ nextQuery: value, nextVideo: "" });
  }

  function changeFilter(value, nextQuery = query) {
    setFilter(value);
    setWatching("");
    setSuggestionsOpen(false);
    writeUrl({ nextQuery, nextFilter: value, nextVideo: "" });
  }

  function openWatch(video, replace = false, source = null) {
    returnFocus.current = source || searchRef.current;
    setWatching(video.filename);
    setSuggestionsOpen(false);
    writeUrl({ nextVideo: video.filename, mode: replace ? "replace" : "push" });
  }

  function closeWatch() {
    setWatching("");
    writeUrl({ nextVideo: "" });
    queueMicrotask(() => (returnFocus.current || searchRef.current)?.focus());
  }

  function chooseSuggestion(item) {
    if (item.type === "Video") {
      openWatch(item.video, false, searchRef.current);
    } else if (item.type === "Category") {
      setQuery("");
      changeFilter(categoryId(item.value), "");
    } else {
      changeQuery(item.value);
      setSuggestionsOpen(false);
    }
  }

  const sections = useMemo(() => {
    if (query) return [["Search Results", filtered]];
    if (filter !== "all") return [[categories.find(([id]) => id === filter)?.[1] || "Videos", filtered]];
    const groups = [];
    const featuredVideos = filtered.filter((video) => video.featured);
    if (featuredVideos.length) groups.push(["Featured", featuredVideos]);
    for (const [, category] of categories) {
      const items = filtered.filter((video) => video.category === category && !video.featured);
      if (items.length) groups.push([category, items]);
    }
    return groups;
  }, [categories, filter, filtered, query]);

  return (
    <>
      <header className="topbar">
        {studioHref && <a className="back-to-studio" href={studioHref}>← Back to Studio</a>}
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">▶</span>
          <span className="brand-name">OmniBioAI <span className="brand-product">Videos</span></span>
        </div>
        <div className="topbar-path" aria-label="Location"><span>{studioHref ? "omnibioai" : "studio"}</span><span className="sep">/</span><span className="current">videos</span></div>
        <div className="topbar-badges"><span className="pill pill-green" id="statusPill" role="status">● {loadState === "loading" ? "LOADING" : loadState === "ready" ? "ONLINE" : loadState === "empty" ? "COMING SOON" : "UNAVAILABLE"}</span><span className="pill pill-orange">BETA</span></div>
      </header>
      <main className="page" id="main">
        <div className="controls" id="controls" hidden={loadState === "empty"}>
          <div className="search-wrap">
            <span className="search-icon" aria-hidden="true" />
            <input ref={searchRef} id="searchInput" value={query} onChange={(event) => changeQuery(event.target.value)}
              onFocus={() => query && setSuggestionsOpen(true)} onBlur={() => setTimeout(() => setSuggestionsOpen(false), 0)}
              onKeyDown={(event) => {
                if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                  event.preventDefault();
                  setSuggestionsOpen(true);
                  setActiveSuggestion((current) => event.key === "ArrowDown"
                    ? (current + 1) % Math.max(suggestions.length, 1)
                    : (current <= 0 ? suggestions.length - 1 : current - 1));
                } else if (event.key === "Enter" && activeSuggestion >= 0) {
                  event.preventDefault();
                  chooseSuggestion(suggestions[activeSuggestion]);
                }
              }}
              placeholder="Search OmniBioAI videos..." autoComplete="off" aria-label="Search OmniBioAI videos"
              role="combobox" aria-autocomplete="list" aria-controls="suggestions"
              aria-expanded={suggestionsOpen && suggestions.length > 0}
              aria-activedescendant={activeSuggestion >= 0 ? `suggestion-${activeSuggestion}` : undefined} />
            <button id="clearSearch" className="clear-search" type="button" aria-label="Clear search" hidden={!query}
              onClick={() => { changeQuery(""); searchRef.current?.focus(); }}>✕</button>
            <div id="suggestions" className="suggestions" role="listbox" aria-label="Search suggestions"
              hidden={!suggestionsOpen || !suggestions.length}>
              {suggestions.map((item, index) => (
                <button className="suggestion" key={`${item.type}-${item.value}`} id={`suggestion-${index}`} type="button" role="option"
                  aria-selected={index === activeSuggestion} onMouseDown={(event) => event.preventDefault()}
                  onClick={() => chooseSuggestion(item)}>
                  <span className="suggestion-kind">{item.type}</span><span className="suggestion-text">{item.label}</span>
                </button>
              ))}
            </div>
          </div>
          <div id="categoryFilters" className="category-filters" role="group" aria-label="Video categories">
            <button type="button" className={`filter-btn${filter === "all" ? " active" : ""}`} aria-pressed={filter === "all"} onClick={() => changeFilter("all")}>All</button>
            {categories.map(([id, label]) => <button key={id} type="button" className={`filter-btn${filter === id ? " active" : ""}`} aria-pressed={filter === id} onClick={() => changeFilter(id)}>{label}</button>)}
          </div>
        </div>

        <div id="library" hidden={Boolean(selected)}>
          <div className="page-heading" onClick={() => setSuggestionsOpen(false)}>
            <div className="heading-row"><h1>Video Tutorials</h1><span className="video-count" id="videoCount">{filtered.length} {filtered.length === 1 ? "video" : "videos"}</span></div>
            <p>Platform walkthroughs, tutorials and workflow demonstrations</p>
          </div>
          <p id="resultStatus" className="result-status" role="status" aria-live="polite" aria-atomic="true">
            {query ? `${filtered.length} result${filtered.length === 1 ? "" : "s"} for “${query}”` : ""}
          </p>
          <div id="content">
            {loadState === "loading" && <div className="state-box"><div className="spinner" /><p>Loading videos...</p></div>}
            {loadState === "error" && <div className="state-box"><div className="state-icon">!</div><h3>Video catalog unavailable</h3><p>Please try again later.</p></div>}
            {loadState === "empty" && <div className="state-box"><div className="state-icon">▶</div><h3>Videos coming soon</h3><p>New OmniBioAI tutorials are being prepared.</p></div>}
            {loadState === "ready" && atHome && featured && <Hero video={featured} onOpen={openWatch} />}
            {loadState === "ready" && filtered.length === 0 && <div className="state-box"><div className="state-icon">⌕</div><h3>No videos found{query ? ` for “${query}”` : ""}</h3><p>Try another search or category.</p><button type="button" onClick={() => changeQuery("")}>Clear search</button></div>}
            {loadState === "ready" && sections.map(([heading, items]) => (
              <section className="library-section" key={heading}><h2>{heading}</h2><div className="video-grid">
                {items.map((video) => <VideoCard key={video.filename} video={video} onOpen={openWatch} />)}
              </div></section>
            ))}
          </div>
        </div>

        <section id="watch" className="watch" aria-labelledby="watchTitle" hidden={!selected}>
          {selected && <>
            <div className="watch-main">
              <button id="watchBack" className="watch-back" type="button" onClick={closeWatch}>← All videos</button>
              <div className="player-frame">
                <video key={selected.filename} id="player" className="player" controls playsInline preload="metadata"
                  src={selected.url} hidden={playerFailed} onError={() => setPlayerFailed(true)} />
                <p id="playerError" className="player-error" role="alert" hidden={!playerFailed}>This video could not be loaded. Please try again later.</p>
              </div>
              <h2 ref={titleRef} id="watchTitle" className="watch-title" tabIndex={-1}>{selected.title}</h2>
              <div className="watch-channel"><span className="channel-avatar" aria-hidden="true">OB</span><div><div className="channel-name">OmniBioAI</div><div className="watch-meta" id="watchMeta">{metaLine(selected)}</div></div></div>
              <div className="watch-description"><p id="watchDesc">{selected.desc}</p><ul id="watchTerms" className="watch-terms" aria-label="Topics">
                {selected.terms.map((term) => <li key={term}><button type="button" className="term-chip" aria-label={`Search videos about ${term}`} onClick={() => { changeQuery(term); searchRef.current?.focus(); }}>#{term}</button></li>)}
              </ul></div>
            </div>
            <aside className="watch-aside" aria-labelledby="upNextHeading"><h2 id="upNextHeading" className="up-next-heading">Up next</h2><div id="upNext" className="up-next">
              {videos.filter((video) => video !== selected).slice(0, 12).map((video) => <VideoCard key={video.filename} video={video} onOpen={openWatch} compactCard />)}
            </div></aside>
          </>}
        </section>
      </main>
    </>
  );
}
