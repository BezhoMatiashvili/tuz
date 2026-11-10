/**
 * Engine section entry point: capability gate, lazy loading, lifecycle and the
 * scroll progress reader.
 *
 * Small on purpose, and it costs a visitor who never scrolls nothing: the
 * page's load event and an idle moment come first, and then three.js and the
 * geometry are only fetched once the section is on screen, or is on the way
 * (within one and a half viewports) and the visitor has started to scroll,
 * touch or type. Start-up itself is cut into small tasks (see pace.js and
 * scene.js). Progress is read in a requestAnimationFrame loop that runs only
 * while the section is on screen. There are no scroll listeners.
 *
 * Tiers: `static` (reduced motion; also what the CSS shows without JS), `lite`
 * (two posters cross-faded by progress) and `3d`. Force one for QA with
 * `?engine=3d`, `?engine=lite` or `?engine=static`.
 */

import { createLabels } from './labels.js';
import { createPacer, inBackground } from './pace.js';
import { beatAt, clamp01, pinsAt, posterAt, scrollProgress, smoothProgress } from './timeline.js';

/**
 * Everything the 3D tier needs, in the order it has to be evaluated. All of it
 * is preloaded together, so the fetches are not a waterfall, and then imported
 * one file at a time, so no task evaluates more than one of them.
 */
const HEAVY = [
  '../../vendor/three@0.187.0/three.core.js',
  '../../vendor/three@0.187.0/three.module.js',
  '../../vendor/three@0.187.0/BufferGeometryUtils.js',
  '../../vendor/three@0.187.0/RoundedBoxGeometry.js',
  './parts.js',
  './materials.js',
  './geometry.js',
  './scene.js',
];
/**
 * How far away the section may be when the visitor starts to move for the 3D
 * tier to start loading. Without any input it has to be on screen itself.
 */
const NEAR = '150% 0px';
const SETTLE_MS = 150;
const IDLE_TIMEOUT_MS = 2000;
/** GPU time per frame above which the resolution drops, and above which 3D is not worth it. */
const SLOW_FRAME_MS = 45;
const HOPELESS_FRAME_MS = 70;
/** Renderers that draw on the CPU: the posters are the better experience there. */
const SOFTWARE = /swiftshader|llvmpipe|softpipe|software|basic render/i;
const INPUTS = ['pointerdown', 'touchstart', 'wheel', 'keydown'];
const INPUT_OPTIONS = { capture: true, passive: true };
/** Re-fire the approach observer as the section moves, so a scroll is noticed without a listener. */
const STEPS = Array.from({ length: 21 }, (_, index) => index / 20);

function softwareRenderer(gl) {
  let name = String(gl.getParameter(gl.RENDERER) || '');
  if (!name || /^webkit webgl$/i.test(name)) {
    const info = gl.getExtension('WEBGL_debug_renderer_info');
    if (info) name = String(gl.getParameter(info.UNMASKED_RENDERER_WEBGL) || '');
  }
  return SOFTWARE.test(name);
}

function boot(root) {
  const stage = root.querySelector('.engine__stage');
  const art = root.querySelector('[data-engine-art]');
  const canvas = root.querySelector('[data-engine-canvas]');
  if (!stage || !art) return;
  const posters = [...root.querySelectorAll('[data-engine-poster]')];
  const beats = [...root.querySelectorAll('.engine__copy [data-engine-beat]')];
  const override = new URLSearchParams(window.location.search).get('engine');
  const fixed =
    root.dataset.engineProgress === undefined ? null : clamp01(Number(root.dataset.engineProgress));
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const labels = createLabels(root);

  let state = '';
  let engine = null;
  let building = null;
  let unwinding = Promise.resolve();
  let context = null;
  let ratioCap = 2;
  let tooSlow = false;
  let frameMs = 0;
  let buildMs = 0;
  let boot = null;
  let baked = null;
  let requested = false;
  let pageReady = false;
  let engaged = false;
  let near = false;
  let onScreen = false;
  let settle = 0;
  let frame = 0;
  let lastTime = 0;
  let dirty = true;
  let progress = fixed ?? 0;
  let drawn = -1;
  let live = -1;
  let step = -1;
  let poster = -1;
  let pinned = -1;
  let layout = 'portrait';
  let width = 0;
  let height = 0;
  let safe = [0, 0, 1, 1];
  let centre = [0.5, 0.5];
  let stickyTop = 0;

  function emit(type) {
    const stats = engine ? engine.stats() : null;
    const detail = { state, layout, progress, frameMs, buildMs, boot, stats };
    if (engine) detail.pins = labels.snapshot(engine.project);
    root.dispatchEvent(new CustomEvent(type, { bubbles: true, detail }));
  }

  function setState(next) {
    if (next === state) return;
    state = next;
    root.dataset.engineState = next;
    poster = -1;
    pinned = -1;
    dirty = true;
    emit('engine:state');
  }

  function measure() {
    width = art.clientWidth;
    height = art.clientHeight;
    layout = width < height ? 'portrait' : 'landscape';
    stickyTop = Number.parseFloat(getComputedStyle(stage).top) || 0;
    // Where copy is laid over the art box, CSS says how much of it to leave free.
    const reserved = Number.parseFloat(getComputedStyle(art).getPropertyValue('--engine-safe-start'));
    safe = [clamp01(reserved || 0), 0, 1, 1];
    centre = [(safe[0] + safe[2]) / 2, 0.5];
  }

  function pixelRatio() {
    const memory = navigator.deviceMemory;
    return Math.min(window.devicePixelRatio || 1, memory && memory <= 4 ? 1.5 : 2, ratioCap);
  }

  function apply(p) {
    const nextStep = beatAt(p, step);
    if (nextStep !== step) {
      step = nextStep;
      root.dataset.engineStep = String(step);
      for (const beat of beats) {
        beat.classList.toggle('is-active', Number(beat.dataset.engineBeat) === step);
      }
    }
    art.style.setProperty('--engine-p', p.toFixed(4));
    if (state === '3d' && engine) {
      // The frame for this progress may already be on the canvas (the one made before the reveal).
      if (p !== live) {
        engine.render(p);
        live = p;
      }
      const nextPinned = pinsAt(p, pinned);
      if (nextPinned !== pinned) {
        pinned = nextPinned;
        labels.show(pinned === 1);
      }
      if (pinned === 1) labels.track(engine.project, width, height, centre);
      return;
    }
    const nextPoster = posterAt(p, poster);
    if (nextPoster !== poster) {
      poster = nextPoster;
      for (const image of posters) {
        image.classList.toggle('is-active', Number(image.dataset.enginePoster) === poster);
      }
    }
    const show = poster === 1 && step === 2 && Boolean(baked && baked[layout]);
    if (show) labels.place(baked[layout], width, height, centre);
    labels.show(show);
  }

  function tick(now) {
    frame = 0;
    const elapsed = lastTime ? now - lastTime : 0;
    lastTime = now;
    let target = fixed;
    let snap = fixed !== null || drawn < 0;
    if (fixed === null) {
      const rect = root.getBoundingClientRect();
      if (rect.bottom <= window.innerHeight) {
        // The wrapper's end is in view: finish, so a fling never strands the stage.
        target = 1;
        snap = true;
      } else {
        target = scrollProgress(rect.top, rect.height, stage.offsetHeight, stickyTop);
        if (rect.top >= stickyTop) snap = true;
      }
    }
    progress = snap ? target : smoothProgress(progress, target, elapsed);
    if (dirty || progress !== drawn) {
      dirty = false;
      apply(progress);
      drawn = progress;
    }
    schedule();
  }

  function schedule() {
    if (!frame && onScreen && !document.hidden && state !== 'static') {
      frame = requestAnimationFrame(tick);
    }
  }

  function halt() {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    lastTime = 0;
  }

  function refresh() {
    dirty = true;
    schedule();
  }

  function loadPins() {
    const source = root.dataset.enginePinsSrc;
    if (baked || !source) return Promise.resolve(baked);
    return fetch(source)
      .then((response) => (response.ok ? response.json() : null))
      .then((json) => {
        baked = json;
        return json;
      })
      .catch(() => null);
  }

  function wants3d() {
    if (!canvas || tooSlow || override === 'lite') return false;
    if (override === '3d') return true;
    const connection = navigator.connection;
    if (connection && connection.saveData) return false;
    return typeof WebGL2RenderingContext !== 'undefined';
  }

  function acquireContext() {
    if (!context) {
      // Sized before the context exists, so the drawing buffer is allocated once.
      const ratio = pixelRatio();
      canvas.width = Math.floor(width * ratio);
      canvas.height = Math.floor(height * ratio);
      try {
        context = canvas.getContext('webgl2', {
          alpha: true,
          antialias: true,
          depth: true,
          stencil: false,
          premultipliedAlpha: true,
          preserveDrawingBuffer: false,
          powerPreference: 'high-performance',
          failIfMajorPerformanceCaveat: override !== '3d',
        });
      } catch {
        context = null;
      }
    }
    return context && !context.isContextLost() ? context : null;
  }

  function teardown() {
    if (building) building.abort();
    building = null;
    if (engine) engine.dispose();
    engine = null;
    live = -1;
    requested = false;
    if (state === '3d') setState('lite');
  }

  async function load() {
    if (requested || state === 'static') return;
    requested = true;
    loadPins().then(refresh);
    measure();
    if (!wants3d() || width < 2 || height < 2) return;
    const controller = new AbortController();
    building = controller;
    const started = performance.now();
    // An aborted start gives the WebGL context back at its next pause, not at
    // once: let it finish before the next one touches the same context.
    const previous = unwinding;
    // Everything in `start` yields at background priority: see pace.js.
    const run = previous.then(() => inBackground(() => start(controller, started)));
    unwinding = run.catch(() => {});
    try {
      await run;
    } catch (error) {
      if (!error || error.name !== 'AbortError') {
        console.warn('[engine] 3D tier unavailable, keeping posters.', error);
      }
    } finally {
      if (building === controller) building = null;
    }
  }

  async function start(controller, started) {
    const { signal } = controller;
    // One pacer for the whole start-up: it throws AbortError at any pause once aborted.
    const pacer = createPacer(signal);
    pacer.mark('context');
    const gl = acquireContext();
    if (!gl) return;
    if (override !== '3d' && softwareRenderer(gl)) {
      tooSlow = true;
      return;
    }
    await pacer.rest();
    for (const path of HEAVY) {
      const link = document.createElement('link');
      link.rel = 'modulepreload';
      link.href = new URL(path, import.meta.url).href;
      document.head.append(link);
    }
    let last = null;
    for (const path of HEAVY) {
      pacer.mark(`import:${path.slice(path.lastIndexOf('/') + 1)}`);
      // The file is evaluated in a task of its own; `wait` carries on in a later one.
      last = await pacer.wait(import(new URL(path, import.meta.url).href));
    }
    const built = await last.createEngineScene({
      canvas,
      context: gl,
      layout,
      width,
      height,
      safe,
      pixelRatio: pixelRatio(),
      signal,
      pacer,
      debug: override === '3d',
      onContextLost() {
        if (state === '3d') setState('lite');
        refresh();
      },
      onContextRestored() {
        live = -1;
        if (engine) setState('3d');
        refresh();
      },
    });
    const discard = () => {
      if (!signal.aborted) return false;
      built.dispose();
      return true;
    };
    if (discard()) return;
    buildMs = performance.now() - started;
    boot = built.boot;
    if (override !== '3d' && fixed === null) {
      // Struggling GPUs: lower the resolution first, then give up.
      frameMs = await built.probe();
      if (discard()) return;
      if (frameMs > HOPELESS_FRAME_MS) {
        tooSlow = true;
        built.dispose();
        return;
      }
      if (frameMs > SLOW_FRAME_MS) ratioCap = Math.max(1, built.pixelRatio - 0.5);
    }
    // The art box may have changed size while the model was being built.
    built.resize(width, height, pixelRatio(), safe);
    // Posters stay up until the first live frame is finished, wherever the
    // visitor has scrolled to in the meantime. It is shown as it is: drawing it
    // again here would make a slow GPU hold up the reveal.
    const first = progress;
    built.render(first);
    await built.settled();
    if (discard()) return;
    engine = built;
    if (gl.isContextLost()) return;
    live = first;
    setState('3d');
    refresh();
    requestAnimationFrame(() => requestAnimationFrame(() => emit('engine:ready')));
  }

  /** The section is in view, or on the way with the visitor moving, and the page has loaded. */
  function wanted() {
    return pageReady && (onScreen || (near && engaged));
  }

  function consider() {
    const due = !requested && state !== 'static' && wanted();
    if (!due) {
      clearTimeout(settle);
      settle = 0;
    } else if (!settle) {
      // Wait a moment: a fling passing through should not start a 700 KB download.
      settle = setTimeout(() => {
        settle = 0;
        if (wanted()) load();
      }, SETTLE_MS);
    }
  }

  function engage() {
    if (engaged) return;
    engaged = true;
    for (const type of INPUTS) window.removeEventListener(type, engage, INPUT_OPTIONS);
    consider();
  }

  // Keyboard focus landing in a beat that is not showing (the legend links live
  // in the last one): bring the page to where that beat is on screen.
  const BEAT_AT = [0.04, 0.45, 0.8];
  root.addEventListener('focusin', (event) => {
    if (state === 'static' || fixed !== null || !(event.target instanceof Element)) return;
    const beat = event.target.closest('[data-engine-beat]');
    if (!beat || !root.contains(beat) || beat.classList.contains('is-active')) return;
    const at = BEAT_AT[Number(beat.dataset.engineBeat)];
    if (at === undefined) return;
    const rect = root.getBoundingClientRect();
    const travel = rect.height - stage.offsetHeight;
    window.scrollTo({ top: window.scrollY + rect.top - stickyTop + travel * at, behavior: 'instant' });
  });

  const seen = new IntersectionObserver((entries) => {
    onScreen = entries[entries.length - 1].isIntersecting;
    if (onScreen) schedule();
    else halt();
    consider();
  });

  const startY = window.scrollY;
  const approach = new IntersectionObserver(
    (entries) => {
      near = entries[entries.length - 1].isIntersecting;
      // The page has moved since it opened, by whatever means.
      if (Math.abs(window.scrollY - startY) > 8) engage();
      if (state === 'static') {
        if (near) showStaticPins();
        return;
      }
      consider();
    },
    { rootMargin: NEAR, threshold: STEPS },
  );

  const resizer = new ResizeObserver(() => {
    const before = layout;
    measure();
    if (state === 'static') return;
    if (layout !== before) {
      // The phone and desktop tiers are different models: rebuild.
      labels.sync();
      teardown();
      consider();
    } else if (engine) {
      engine.resize(width, height, pixelRatio(), safe);
      live = -1;
    }
    refresh();
  });

  function enterScroll() {
    root.dataset.engineMode = 'scroll';
    labels.sync();
    measure();
    setState('lite');
    apply(progress);
    seen.observe(root);
    approach.observe(root);
    resizer.observe(art);
    if (fixed !== null) requestAnimationFrame(() => emit('engine:ready'));
  }

  let staticPins = false;
  function showStaticPins() {
    if (staticPins) return;
    staticPins = true;
    loadPins().then((json) => {
      if (state !== 'static') return;
      if (json) labels.pinStatic(json);
      emit('engine:ready');
    });
  }

  function enterStatic() {
    halt();
    clearTimeout(settle);
    settle = 0;
    seen.disconnect();
    approach.disconnect();
    resizer.disconnect();
    teardown();
    root.dataset.engineMode = 'static';
    for (const beat of beats) beat.classList.remove('is-active');
    for (const image of posters) image.classList.remove('is-active');
    labels.sync();
    setState('static');
    // The pin positions are only fetched once the section is on its way too.
    staticPins = false;
    approach.observe(root);
  }

  function choose() {
    const still = override === 'static' || (motion.matches && override !== '3d' && override !== 'lite');
    if (still) enterStatic();
    else enterScroll();
  }

  motion.addEventListener('change', choose);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) halt();
    else schedule();
  });
  window.addEventListener('pagehide', () => {
    halt();
    teardown();
  });
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) consider();
    schedule();
  });
  for (const type of INPUTS) window.addEventListener(type, engage, INPUT_OPTIONS);

  // Nothing heavy before the page has loaded and had a quiet moment.
  const settled = () => {
    const go = () => {
      pageReady = true;
      consider();
    };
    if (typeof window.requestIdleCallback === 'function') {
      window.requestIdleCallback(go, { timeout: IDLE_TIMEOUT_MS });
    } else {
      setTimeout(go, 200);
    }
  };
  if (document.readyState === 'complete') settled();
  else window.addEventListener('load', settled, { once: true });

  choose();
}

for (const root of document.querySelectorAll('[data-engine]')) boot(root);
