// Small DOM helpers. Text always goes in as text nodes, never as markup, and nothing here
// writes an inline style attribute (the page's CSP forbids them).

const SVG_NS = 'http://www.w3.org/2000/svg';
const BOOLEAN = new Set(['disabled', 'hidden', 'checked', 'required', 'readonly', 'multiple',
  'selected', 'open', 'novalidate', 'autofocus']);

function append(parent, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === '') {
      continue;
    }
    if (Array.isArray(child)) {
      append(parent, child);
    } else if (child instanceof Node) {
      parent.appendChild(child);
    } else {
      parent.appendChild(document.createTextNode(String(child)));
    }
  }
}

function apply(node, props, svg) {
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) {
      continue;
    }
    if (key === 'style') {
      throw new Error('Inline style attributes are not allowed here');
    }
    if (key === 'class') {
      node.setAttribute('class', Array.isArray(value) ? value.filter(Boolean).join(' ') : value);
    } else if (key === 'text') {
      node.textContent = String(value);
    } else if (key === 'dataset') {
      for (const [name, item] of Object.entries(value)) {
        if (item !== null && item !== undefined && item !== false) {
          node.dataset[name] = String(item);
        }
      }
    } else if (key === 'on') {
      for (const [type, handler] of Object.entries(value)) {
        if (handler) {
          node.addEventListener(type, handler);
        }
      }
    } else if (!svg && key === 'value') {
      node.value = value;
    } else if (!svg && BOOLEAN.has(key)) {
      node.toggleAttribute(key, Boolean(value));
      if (key in node) {
        node[key] = Boolean(value);
      }
    } else {
      node.setAttribute(key, value === true ? '' : String(value));
    }
  }
}

/** Create an HTML element: h('button', {class: 'btn', on: {click}}, 'Save'). */
export function h(tag, props, ...children) {
  const node = document.createElement(tag);
  apply(node, props, false);
  append(node, children);
  return node;
}

/** Create an SVG element. */
export function s(tag, props, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  apply(node, props, true);
  append(node, children);
  return node;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function mount(node, ...children) {
  node.replaceChildren();
  append(node, children);
  return node;
}

let sequence = 0;
/** A document-unique id for label/input and aria wiring. */
export function uid(prefix = 'id') {
  sequence += 1;
  return `${prefix}-${sequence}`;
}

// Icons: 24px grid, drawn with strokes in the current text colour.
const ICONS = {
  overview: ['M4 4h7v9H4z', 'M13 4h7v5h-7z', 'M13 11h7v9h-7z', 'M4 15h7v5H4z'],
  inventory: ['M3.5 8 12 3.5 20.5 8v8L12 20.5 3.5 16z', 'M3.5 8 12 12.5 20.5 8', 'M12 12.5v8'],
  content: ['M6 3.5h8l4 4v13H6z', 'M14 3.5v4h4', 'M9 12h6', 'M9 16h6'],
  more: ['M4 12a1 1 0 1 0 2 0 1 1 0 0 0-2 0z', 'M11 12a1 1 0 1 0 2 0 1 1 0 0 0-2 0z', 'M18 12a1 1 0 1 0 2 0 1 1 0 0 0-2 0z'],
  users: ['M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z', 'M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6',
    'M16 4.3a3.5 3.5 0 0 1 0 6.4', 'M18 14.3c2.1.7 3.5 2.6 3.5 5.7'],
  audit: ['M3.5 12a8.5 8.5 0 1 0 2.6-6.1', 'M3.5 4.5v4h4', 'M12 7.5V12l3 2'],
  account: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M12 12.5a3.25 3.25 0 1 0 0-6.5 3.25 3.25 0 0 0 0 6.5z',
    'M6.2 18.9c.9-2.3 3.1-3.9 5.8-3.9s4.9 1.6 5.8 3.9'],
  globe: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M3 12h18',
    'M12 3c2.5 2.4 3.8 5.5 3.8 9s-1.3 6.6-3.8 9c-2.5-2.4-3.8-5.5-3.8-9S9.5 5.4 12 3z'],
  signout: ['M10 4H5.5A1.5 1.5 0 0 0 4 5.5v13A1.5 1.5 0 0 0 5.5 20H10', 'M15 16l4-4-4-4', 'M19 12H9'],
  external: ['M14 4h6v6', 'M20 4l-9 9', 'M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10'],
  search: ['M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14z', 'M20 20l-4-4'],
  check: ['M5 12.5l4.5 4.5L19 7.5'],
  'chevron-right': ['M9 5l7 7-7 7'],
  'chevron-left': ['M15 5l-7 7 7 7'],
  'arrow-up': ['M12 19V5', 'M6 11l6-6 6 6'],
  'arrow-down': ['M12 5v14', 'M6 13l6 6 6-6'],
  'arrow-left': ['M19 12H5', 'M11 6l-6 6 6 6'],
  'arrow-right': ['M5 12h14', 'M13 6l6 6-6 6'],
  plus: ['M12 5v14', 'M5 12h14'],
  trash: ['M4 7h16', 'M9 7V4.5h6V7', 'M6.5 7l1 13h9l1-13', 'M10 11v5.5', 'M14 11v5.5'],
  edit: ['M4 20h4L19 9l-4-4L4 16z', 'M13 7l4 4'],
  eye: ['M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z', 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z'],
  'eye-off': ['M4 4l16 16', 'M9.9 5.8c.7-.2 1.4-.3 2.1-.3 6 0 9.5 6.5 9.5 6.5a17 17 0 0 1-3 3.8',
    'M6.3 7.3A16.6 16.6 0 0 0 2.5 12S6 18.5 12 18.5c1.4 0 2.7-.3 3.8-.8', 'M9.9 9.9a3 3 0 0 0 4.2 4.2'],
  copy: ['M9 9h10.5v10.5H9z', 'M5 15h-.5V4.5H15V5'],
  refresh: ['M20 12a8 8 0 0 1-14.3 4.9', 'M4 12a8 8 0 0 1 14.3-4.9', 'M18.5 3.5v4h-4', 'M5.5 20.5v-4h4'],
  upload: ['M12 16V4', 'M7 9l5-5 5 5', 'M4 16v3.5h16V16'],
  image: ['M4 5h16v14H4z', 'M4 16l4.5-4.5 4 4 3-3L20 17', 'M15.5 9.5h.01'],
  table: ['M4 5h16v14H4z', 'M4 10h16', 'M4 14.5h16', 'M10 5v14'],
  chart: ['M5 20V10', 'M12 20V4', 'M19 20v-7'],
  clock: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M12 7v5l3.5 2'],
  key: ['M8 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z', 'M11.5 12H21', 'M18 12v3', 'M15 12v2'],
  spark: ['M12 3v4', 'M12 17v4', 'M3 12h4', 'M17 12h4', 'M6 6l2.5 2.5', 'M15.5 15.5 18 18', 'M18 6l-2.5 2.5', 'M8.5 15.5 6 18'],
  circle: ['M12 20a8 8 0 1 0 0-16 8 8 0 0 0 0 16z'],
  info: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M12 11v5.5', 'M12 7.8h.01'],
  lock: ['M6 11h12v9H6z', 'M8.5 11V8a3.5 3.5 0 0 1 7 0v3'],
  // Live chats: the alarm bell, a conversation, a phone handset, the alarm sound on and off.
  bell: ['M6 17v-6a6 6 0 0 1 12 0v6l1.5 2h-15z', 'M10 21a2 2 0 0 0 4 0'],
  chat: ['M4 5.5h16v11H11l-4.5 3.5v-3.5H4z', 'M8 9.5h8', 'M8 12.5h5'],
  phone: ['M5 4.5h3.2l1.6 4-2 1.3a11 11 0 0 0 5.4 5.4l1.3-2 4 1.6V18a1.5 1.5 0 0 1-1.5 1.5A14.5 14.5 0 0 1 3.5 6 1.5 1.5 0 0 1 5 4.5z'],
  speaker: ['M4 9.5h3.5L12 6v12l-4.5-3.5H4z', 'M15.5 9.5a3.5 3.5 0 0 1 0 5', 'M18 7a7 7 0 0 1 0 10'],
  'speaker-off': ['M4 9.5h3.5L12 6v12l-4.5-3.5H4z', 'M16 9.5l5 5', 'M21 9.5l-5 5'],
  close: ['M6 6l12 12', 'M18 6 6 18'],
};

// Status glyphs are filled shapes with a contrasting mark, so they read by shape as well
// as by colour. Each entry: [shape path, mark paths].
const STATUS = {
  good: ['M12 21.5a9.5 9.5 0 1 0 0-19 9.5 9.5 0 0 0 0 19z', ['M7.8 12.3l2.9 2.9 5.5-5.7']],
  warning: ['M12 2.8 1.8 20.5h20.4z', ['M12 9.5v5', 'M12 17.5h.01']],
  critical: ['M8 2.5h8l5.5 5.5v8L16 21.5H8L2.5 16V8z', ['M12 7.5v5.5', 'M12 16.5h.01']],
  neutral: ['M12 21.5a9.5 9.5 0 1 0 0-19 9.5 9.5 0 0 0 0 19z', ['M8 12h8']],
  pending: ['M12 21.5a9.5 9.5 0 1 0 0-19 9.5 9.5 0 0 0 0 19z', ['M12 7.5v5l3 1.8']],
  unknown: ['M12 21.5a9.5 9.5 0 1 0 0-19 9.5 9.5 0 0 0 0 19z',
    ['M9.6 9.6a2.5 2.5 0 1 1 3.6 2.2c-.8.4-1.2.9-1.2 1.7', 'M12 16.7h.01']],
};

/** A decorative stroke icon. Pair it with visible text or an aria-label on the control. */
export function icon(name, size = 20) {
  const paths = ICONS[name] || ICONS.circle;
  return s('svg', {
    class: 'icon', viewBox: '0 0 24 24', width: size, height: size, 'aria-hidden': 'true',
    focusable: 'false',
  }, paths.map((d) => s('path', { d })));
}

/** A filled status glyph: good, warning, critical, neutral, pending or unknown. */
export function statusIcon(kind, size = 18) {
  const [shape, marks] = STATUS[kind] || STATUS.unknown;
  return s('svg', {
    class: `status-icon status-icon--${STATUS[kind] ? kind : 'unknown'}`, viewBox: '0 0 24 24',
    width: size, height: size, 'aria-hidden': 'true', focusable: 'false',
  }, s('path', { class: 'status-icon__shape', d: shape }),
  marks.map((d) => s('path', { class: 'status-icon__mark', d })));
}
