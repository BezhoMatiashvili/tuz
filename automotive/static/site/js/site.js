/**
 * The one script the landing page loads eagerly. It wires the phone menu, the scroll
 * reveals, the gallery buttons and the chat launcher, and loads the two heavier modules
 * only when they are wanted: the part search when its section is near (or the browser is
 * idle), the chat when a visitor reaches for it. There are no scroll listeners here:
 * everything that depends on position uses IntersectionObserver.
 */

import { data, reducedMotion } from './dom.js';

const root = document.documentElement;
// Tells the stylesheet the reveals are in hand (see "reveals" in site.css).
root.dataset.js = '';

/* ------------------------------------------------------------ phone menu */

const menu = document.getElementById('menu');
const menuButton = document.querySelector('[data-menu-open]');
if (menu && menuButton && typeof menu.showModal === 'function') {
  menuButton.setAttribute('aria-expanded', 'false');
  menuButton.addEventListener('click', () => {
    menu.showModal();
    menuButton.setAttribute('aria-expanded', 'true');
  });
  // A link closes the sheet and then follows its anchor.
  menu.addEventListener('click', (event) => {
    if (event.target.closest('[data-menu-close]')) menu.close();
  });
  menu.addEventListener('close', () => menuButton.setAttribute('aria-expanded', 'false'));
  window.matchMedia('(min-width: 1024px)').addEventListener('change', (query) => {
    if (query.matches && menu.open) menu.close();
  });
}

/* ---------------------------------------------------------------- reveals */

const revealed = new IntersectionObserver(
  (entries, observer) => {
    let position = 0;
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const node = entry.target;
      observer.unobserve(node);
      // Things that arrive together follow each other by 50 ms, six at most.
      const delay = `${Math.min(position, 5) * 50}ms`;
      node.style.animationDelay = delay;
      node.style.transitionDelay = delay;
      position += 1;
      node.dataset.visible = '';
      // Afterwards drop the hooks, so nothing keeps a clip-path it no longer needs.
      window.setTimeout(() => {
        node.style.animationDelay = '';
        node.style.transitionDelay = '';
        node.removeAttribute('data-reveal');
        node.removeAttribute('data-visible');
        document.dispatchEvent(new CustomEvent('reveal:done'));
      }, 1500);
    }
  },
  { rootMargin: '0px 0px -10% 0px', threshold: 0.12 },
);
for (const node of document.querySelectorAll('[data-reveal]')) revealed.observe(node);

/* ------------------------------------------------------------ part search */

const parts = document.getElementById('parts');
let search = null;

/** Load the part search once. `near` also fetches the make list. */
function loadSearch(near) {
  search ||= import('./search.js').then((module) => {
    module.init();
    return module;
  });
  if (near) search.then((module) => module.prepare());
  return search;
}

if (parts) {
  const approaching = new IntersectionObserver(
    (entries, observer) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      observer.disconnect();
      loadSearch(true);
    },
    { rootMargin: '900px 0px' },
  );
  approaching.observe(parts);
  const idle = window.requestIdleCallback || ((work) => window.setTimeout(work, 2000));
  window.addEventListener('load', () => idle(() => loadSearch(false), { timeout: 5000 }), { once: true });
}

// Any part link on the page (the engine legend, for one) searches for that part by name.
document.addEventListener('click', (event) => {
  const link = event.target.closest('[data-part-query]');
  if (!link || !parts) return;
  const query = link.dataset.partQuery;
  // A link to #parts scrolls there by itself; anything else is taken there.
  if (!link.matches('a[href="#parts"]')) {
    parts.scrollIntoView({ behavior: reducedMotion.matches ? 'auto' : 'smooth' });
  }
  loadSearch(true).then((module) => module.searchFor(query));
});

/* ------------------------------------------------------------------- chat */

const launcher = document.querySelector('[data-chat-launcher]');
const chatOn = Boolean(data.chat && data.chat.enabled) && Boolean(document.getElementById('chat'));
let chat = null;
const loadChat = () => (chat ||= import('./chat.js'));

if (chatOn) {
  // Intent: fetch the module as the pointer or finger reaches the launcher.
  for (const type of ['pointerenter', 'touchstart', 'focus']) {
    launcher?.addEventListener(type, loadChat, { once: true, passive: true });
  }
  document.addEventListener('click', (event) => {
    const opener = event.target.closest('[data-chat-open]');
    if (!opener) return;
    event.preventDefault();
    loadChat().then((module) => module.open({ opener, prefill: opener.dataset.chatPrefill || '' }));
  });
} else {
  // The server renders no chat controls when the chat is off; hide any that slipped in.
  for (const node of document.querySelectorAll('[data-chat-open]')) node.hidden = true;
}

/* The launcher never covers a button, a field, a price or the logo. It steps aside while
   one of them passes under its corner, while a search field has focus (the keyboard is up)
   and while the chat is open. Prices keep clear of it through padding in the stylesheet. */
if (launcher) {
  const reasons = new Set();
  const settle = () => launcher.toggleAttribute('data-away', reasons.size > 0);
  const flag = (reason, on) => {
    if (on) reasons.add(reason);
    else reasons.delete(reason);
    settle();
  };

  const fields = 'input:not([type="radio"]), select, textarea';
  parts?.addEventListener('focusin', (event) => flag('field', event.target.matches(fields)));
  parts?.addEventListener('focusout', () => flag('field', false));
  document.addEventListener('chat:state', (event) => flag('chat', event.detail.open));

  const CORNER = 78; // 56 px button, 16 px inset, 6 px of air
  let corner = null;
  let size = '';
  const watchCorner = () => {
    const width = root.clientWidth;
    const height = window.innerHeight;
    if (size === `${width}x${height}`) return;
    size = `${width}x${height}`;
    corner?.disconnect();
    // A new observer reports every control once, so the flag settles without a flicker.
    const under = new Set();
    // The observer's root is shrunk to the launcher's corner of the viewport.
    corner = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          // A numbered pin of the engine counts only while the pins are showing.
          const showing = !entry.target.closest('.engine__pin') || entry.target.closest('.engine__pins.is-visible');
          if (showing && entry.isIntersecting && entry.intersectionRect.width > 1 && entry.intersectionRect.height > 1) {
            under.add(entry.target);
          } else {
            under.delete(entry.target);
          }
        }
        flag('covered', under.size > 0);
      },
      { rootMargin: `-${Math.max(0, height - CORNER)}px 0px 0px -${Math.max(0, width - CORNER)}px` },
    );
    // Results are not watched: the stylesheet keeps them clear of the corner with padding.
    // A pin is a point; what has a size is its number and its tag.
    const controls = 'main a[href], main button, main .input, main .seg__opt, footer a, '
      + '#engine .engine__pin-dot, #engine .engine__pin-tag, .hero__logo';
    for (const node of document.querySelectorAll(controls)) {
      if (!node.closest('.results')) corner.observe(node);
    }
  };
  watchCorner();
  let queued = 0;
  const again = (fresh) => {
    if (fresh) size = '';
    window.cancelAnimationFrame(queued);
    queued = window.requestAnimationFrame(watchCorner);
  };
  window.addEventListener('resize', () => again(false));
  // A wipe changes what is visible without a scroll: look again once it has finished.
  document.addEventListener('reveal:done', () => again(true));
  // So do the engine's pins, which are built late and shown only near the end of its scroll.
  const pins = document.querySelector('#engine .engine__pins');
  if (pins) {
    new MutationObserver(() => again(true)).observe(pins, { attributes: true, attributeFilter: ['class'], childList: true });
  }
}

/* ---------------------------------------------------------------- gallery */

const row = document.querySelector('.gallery__row');
const galleryNav = document.querySelector('.gallery__nav');
if (row && galleryNav) {
  const [back, forward] = galleryNav.querySelectorAll('button');
  const items = row.querySelectorAll('.gallery__item');
  new ResizeObserver(() => {
    galleryNav.hidden = row.scrollWidth <= row.clientWidth + 4;
  }).observe(row);
  // The first and last photo tell the buttons when an end is reached.
  const ends = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        const atEnd = entry.intersectionRatio > 0.98;
        if (entry.target === items[0]) back.disabled = atEnd;
        if (entry.target === items[items.length - 1]) forward.disabled = atEnd;
      }
    },
    { root: row, threshold: [0, 0.98, 1] },
  );
  if (items.length) {
    ends.observe(items[0]);
    ends.observe(items[items.length - 1]);
  }
  galleryNav.addEventListener('click', (event) => {
    const button = event.target.closest('[data-gallery]');
    if (!button || !items.length) return;
    const step = items[0].getBoundingClientRect().width + 12;
    row.scrollBy({
      left: step * Number(button.dataset.gallery),
      behavior: reducedMotion.matches ? 'auto' : 'smooth',
    });
  });
}

/* ------------------------------------------------------------------ footer */

for (const node of document.querySelectorAll('[data-year]')) {
  if (!node.textContent.trim()) node.textContent = String(new Date().getFullYear());
}
