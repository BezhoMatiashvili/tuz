/**
 * Dev harness for the engine section. Loaded before js/engine/index.js.
 *
 *   ?engine=3d|lite|static   force a tier (read by index.js itself)
 *   ?p=0.8                   pin the section to a fixed progress
 *   ?poster=1&layout=...     only the art box, on a transparent page
 *   ?hud=1                   live readout of state, progress and draw stats
 *   ?lose=1                  drop and restore the WebGL context once ready
 *   ?tall=1                  four screens of filler above, to test lazy loading
 *   ?site=1                  load the real css/site.css instead of the stand-ins
 *   ?lang=ka                 Georgian copy (and, with site=1, the Georgian display face)
 *
 * Ready marker for scripted screenshots: <html data-engine-ready="1">, with
 * data-engine-state, data-engine-stats, data-engine-baked (pin positions) and
 * data-engine-boot (how the 3D start-up went) alongside.
 *
 * The 3D tier only starts after the load event and an idle moment, and then
 * only if the section is on screen or the page has been scrolled or touched.
 */

const params = new URLSearchParams(window.location.search);
const root = document.querySelector('[data-engine]');
const html = document.documentElement;
const hud = document.querySelector('[data-dev-hud]');

if (params.has('p')) root.dataset.engineProgress = params.get('p');
if (params.get('poster') === '1') html.classList.add('is-poster');
if (params.get('tall') === '1') html.classList.add('is-tall');

// ?site=1: the real global stylesheet instead of the stand-ins, linked ahead of
// engine.css as in production. ?lang=ka: the Georgian copy and display face.
if (params.get('site') === '1') {
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = new URL('../css/site.css', import.meta.url).href;
  document.querySelector('link[href$="engine.css"]').before(link);
  html.classList.add('is-site');
}
if (params.get('lang') === 'ka') {
  html.lang = 'ka';
  for (const node of document.querySelectorAll('[data-ka]')) node.textContent = node.dataset.ka;
}

const wanted = params.get('engine');
let last = null;

function publish(detail) {
  last = detail;
  html.dataset.engineState = detail.state;
  html.dataset.engineLayout = detail.layout;
  if (detail.stats) html.dataset.engineStats = JSON.stringify(detail.stats);
  if (detail.boot) html.dataset.engineBoot = JSON.stringify(detail.boot);
  if (detail.pins) html.dataset.engineBaked = JSON.stringify(detail.pins);
}

root.addEventListener('engine:state', (event) => publish(event.detail));

root.addEventListener('engine:ready', (event) => {
  publish(event.detail);
  // When a tier is forced, only that tier counts as ready.
  if (wanted && wanted !== event.detail.state) return;
  const layout = params.get('layout');
  if (layout && layout !== event.detail.layout) {
    console.error(`harness: asked for the ${layout} layout but the art box is ${event.detail.layout}`);
  }
  html.dataset.engineReady = '1';
  if (params.get('lose') === '1' && event.detail.state === '3d' && !html.dataset.engineLost) {
    html.dataset.engineLost = 'pending';
    const canvas = root.querySelector('[data-engine-canvas]');
    const extension = canvas.getContext('webgl2').getExtension('WEBGL_lose_context');
    extension.loseContext();
    setTimeout(() => {
      html.dataset.engineLost = 'lost';
      setTimeout(() => {
        extension.restoreContext();
        setTimeout(() => {
          html.dataset.engineLost = 'restored';
        }, 600);
      }, 400);
    }, 400);
  }
});

if (params.get('hud') === '1' && hud) {
  hud.hidden = false;
  const art = root.querySelector('[data-engine-art]');
  const draw = () => {
    const progress = Number(getComputedStyle(art).getPropertyValue('--engine-p')) || 0;
    const stats = last && last.stats;
    hud.textContent = [
      `state  ${root.dataset.engineState || '-'}  ${last ? last.layout : ''}`,
      `p      ${progress.toFixed(3)}  beat ${root.dataset.engineStep || '-'}`,
      stats ? `draw   ${stats.calls} calls  ${stats.triangles} tris` : 'draw   -',
      last && last.frameMs ? `probe  ${last.frameMs.toFixed(1)} ms of GPU time per frame` : 'probe  -',
      last && last.buildMs ? `build  ${last.buildMs.toFixed(0)} ms from trigger to ready` : 'build  -',
      last && last.boot
        ? `tasks  ${last.boot.slices} slices, longest ${last.boot.longest.toFixed(1)} ms (${last.boot.at})`
        : 'tasks  -',
    ].join('\n');
    requestAnimationFrame(draw);
  };
  requestAnimationFrame(draw);
}
