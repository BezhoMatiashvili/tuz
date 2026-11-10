// Gallery (admin): photos shown on the public site. Uploads are decoded in the browser,
// drawn to a canvas at no more than 1600 px and re-encoded as JPEG, which also strips
// location details. The canvas doubles as the preview while the upload runs.

import { ApiError, api, describeError } from '../api.js';
import { h, mount, statusIcon } from '../dom.js';
import { countChars, fieldMessage, limitedInput, normalize } from '../forms.js';
import { bytes, percent } from '../format.js';
import { getLang, t } from '../strings.js';
import {
  adminOnly, announce, busy, button, card, confirmDialog, emptyState, errorState, iconButton,
  isForbidden, loadingBlock, skeleton, switchControl, toast, toastError,
} from '../ui.js';

const MAX_SIDE = 1600;
const MAX_BYTES = 1500000; // The server accepts up to 1.5 MiB.
const QUALITIES = [0.85, 0.75, 0.65, 0.5];
const ACCEPT = 'image/jpeg,image/png,image/webp';
const ALT_MAX = 160;

/** True for a photo description field (they carry data-alt). */
function isAltField(node) {
  return node instanceof HTMLElement && Boolean(node.dataset.alt);
}

class UploadProblem extends Error {
  constructor(reason) {
    super(reason);
    this.reason = reason;
  }
}

function encode(canvas, quality) {
  return new Promise((resolve) => {
    canvas.toBlob(resolve, 'image/jpeg', quality);
  });
}

/** Decode, scale and re-encode one chosen file. Returns {canvas, jpeg}. */
async function prepare(file) {
  let bitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    throw new UploadProblem('read');
  }
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const width = Math.max(1, Math.round(bitmap.width * scale));
  const height = Math.max(1, Math.round(bitmap.height * scale));
  const canvas = h('canvas', { class: 'photo__image', width, height });
  const context = canvas.getContext('2d');
  context.fillStyle = '#ffffff'; // JPEG has no transparency: give see-through areas a white base.
  context.fillRect(0, 0, width, height);
  context.drawImage(bitmap, 0, 0, width, height);
  if (bitmap.close) {
    bitmap.close();
  }
  for (const quality of QUALITIES) {
    const jpeg = await encode(canvas, quality);
    if (jpeg && jpeg.size <= MAX_BYTES) {
      return { canvas, jpeg };
    }
  }
  throw new UploadProblem('large');
}

export function mountView(root, ctx) {
  const state = { status: 'loading', error: null, media: [], uploads: [] };
  const drafts = new Map(); // "<media id>:<lang>" -> description text typed but not saved yet
  let alive = true;
  let running = false;
  let picker = null;
  let sequence = 0;
  ctx.setGuard(null);

  const byId = (id) => state.media.find((row) => row.media_id === id);
  const replace = (saved) => {
    state.media = state.media.map((row) => (row.media_id === saved.media_id ? saved : row));
  };
  const altOf = (row, index) => row[`alt_${getLang()}`] || row.alt_ka || row.alt_en
    || t('gallery.photo', { n: index + 1 });

  // -- uploads -------------------------------------------------------------------------

  function uploadCard(item) {
    const status = h('p', { class: 'upload__status', text: t('gallery.preparing') });
    const fill = h('span', { class: 'progress__bar' });
    const progress = h('div', {
      class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100',
      'aria-valuenow': '0', 'aria-label': item.name,
    }, fill);
    const frame = h('div', { class: 'photo__frame photo__frame--pending' }, skeleton('image'));
    const body = h('div', { class: 'photo__body' },
      h('p', { class: 'upload__name', text: item.name }), progress, status);
    item.node = h('li', { class: 'photo photo--upload' }, frame, body);
    item.preview = (canvas) => mount(frame, canvas);
    item.progress = (fraction) => {
      const value = Math.max(0, Math.min(1, fraction));
      fill.style.transform = `scaleX(${value})`;
      progress.setAttribute('aria-valuenow', String(Math.round(value * 100)));
      status.textContent = t('gallery.uploading', { percent: percent(value, 0) });
    };
    item.fail = (message) => {
      const dismiss = button(t('common.close'), {
        small: true, 'aria-label': t('gallery.dismiss'),
        onClick: () => {
          state.uploads = state.uploads.filter((other) => other !== item);
          drawGrid({ add: true });
        },
      });
      mount(body,
        h('p', { class: 'upload__name', text: item.name }),
        h('p', { class: 'field__error', role: 'alert' }, statusIcon('critical', 16), h('span', { text: message })),
        dismiss);
    };
    return item.node;
  }

  async function runQueue() {
    if (running) {
      return;
    }
    running = true;
    for (;;) {
      const item = state.uploads.find((entry) => entry.stage === 'waiting');
      if (!item) {
        break;
      }
      item.stage = 'working';
      try {
        const { canvas, jpeg } = await prepare(item.file);
        item.preview(canvas);
        item.progress(0);
        const saved = await api.uploadMedia(jpeg, 'image/jpeg', item.progress);
        state.uploads = state.uploads.filter((other) => other !== item);
        if (byId(saved.media_id)) {
          toast(t('gallery.duplicate'), 'info');
        } else {
          state.media = [...state.media, saved];
          toast(t('gallery.uploaded'));
        }
        if (alive) {
          // Land on the new photo's switch unless the person is busy typing elsewhere.
          drawGrid(isAltField(document.activeElement) ? null : { id: saved.media_id, part: 'switch' });
        }
      } catch (error) {
        item.stage = 'failed';
        let message = describeError(error);
        if (error instanceof UploadProblem) {
          message = t(error.reason === 'large' ? 'gallery.err_large' : 'gallery.err_read');
        } else if (error instanceof ApiError && error.status === 413) {
          message = t('gallery.err_large');
        }
        item.fail(message);
      }
      item.file = null;
    }
    running = false;
  }

  function chosen(files) {
    for (const file of files) {
      sequence += 1;
      const item = { key: sequence, name: file.name || t('gallery.photo', { n: sequence }), file, stage: 'waiting' };
      uploadCard(item);
      state.uploads.push(item);
    }
    if (state.status === 'ready') {
      drawGrid(null);
    }
    runQueue();
  }

  // -- photo actions -------------------------------------------------------------------

  async function reorder(index, delta) {
    const target = index + delta;
    const before = [...state.media];
    const next = [...state.media];
    [next[index], next[target]] = [next[target], next[index]];
    state.media = next;
    drawGrid({ id: next[target].media_id, part: delta < 0 ? 'earlier' : 'later' });
    announce(t('common.moved', { position: target + 1, total: next.length }));
    try {
      await api.orderMedia(next.map((row) => row.media_id));
    } catch (error) {
      if (!alive) {
        return;
      }
      state.media = before;
      drawGrid(null);
      toastError(error);
    }
  }

  async function publish(id, checked) {
    try {
      replace(await api.updateMedia(id, { is_published: checked }));
      toast(t(checked ? 'gallery.shown' : 'gallery.hidden_toast'));
      if (alive) {
        drawGrid({ id, part: 'switch' });
      }
    } catch (error) {
      toastError(error);
      if (error instanceof ApiError && error.status === 404) {
        load();
      }
      throw error; // The switch goes back.
    }
  }

  async function remove(id, trigger) {
    const sure = await confirmDialog({
      title: t('gallery.delete_title'), text: t('gallery.delete_text'),
      confirmLabel: t('common.delete'), danger: true, opener: trigger,
    });
    if (!sure) {
      return;
    }
    const restore = busy(trigger);
    try {
      await api.deleteMedia(id);
    } catch (error) {
      restore();
      if (!(error instanceof ApiError && error.status === 404)) {
        toastError(error);
        return;
      }
    }
    const index = state.media.findIndex((row) => row.media_id === id);
    state.media = state.media.filter((row) => row.media_id !== id);
    toast(t('gallery.deleted'));
    const neighbour = state.media[Math.min(index, state.media.length - 1)];
    drawGrid(neighbour ? { id: neighbour.media_id, part: 'remove' } : { add: true });
  }

  /** A description input that saves itself when the cursor leaves it (or on Enter). */
  function altField(row, lang, image, index) {
    const key = `alt_${lang}`;
    const draftKey = `${row.media_id}:${lang}`;
    const control = limitedInput({
      label: t('gallery.alt'), badge: t(`common.${lang}`), lang, max: ALT_MAX,
      value: drafts.has(draftKey) ? drafts.get(draftKey) : (row[key] || ''),
      onInput: (value) => {
        drafts.set(draftKey, value); // Survives a redraw caused by an upload finishing.
        control.setError('');
      },
    });
    control.input.dataset.alt = draftKey;
    let saving = false;
    const save = async () => {
      const current = byId(row.media_id);
      const typed = control.input.value;
      if (saving || !current || normalize(typed) === (current[key] || '')) {
        drafts.delete(draftKey);
        return;
      }
      saving = true;
      try {
        const saved = await api.updateMedia(row.media_id, { [key]: typed });
        replace(saved);
        if (control.input.value === typed) {
          drafts.delete(draftKey);
          if (document.activeElement !== control.input) {
            control.input.value = saved[key] || '';
          }
        }
        image.alt = altOf(saved, index);
        toast(t('common.saved'));
      } catch (error) {
        if (error instanceof ApiError && error.code === 'INVALID_FIELDS') {
          // The server only says "invalid" here; a text over the limit deserves the plain reason.
          const tooLong = countChars(typed) > ALT_MAX;
          control.setError(fieldMessage(tooLong ? 'TOO_LONG' : (error.fields[key] || 'INVALID')));
        } else {
          toastError(error);
        }
      }
      saving = false;
    };
    control.input.addEventListener('blur', save);
    control.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        save();
      }
    });
    return control.node;
  }

  // -- drawing -------------------------------------------------------------------------

  function drawGrid(focus) {
    // A redraw that nobody asked for (an upload finished) must not take the cursor away
    // from a description being typed.
    const active = document.activeElement;
    const typing = !focus && isAltField(active) ? {
      key: active.dataset.alt, start: active.selectionStart, end: active.selectionEnd,
    } : null;
    picker = h('input', { type: 'file', accept: ACCEPT, multiple: true, hidden: true, tabindex: '-1' });
    picker.addEventListener('change', () => {
      const files = [...picker.files];
      picker.value = '';
      if (files.length) {
        chosen(files);
      }
    });
    const add = button(t('gallery.upload'), {
      variant: 'primary', iconName: 'upload', onClick: () => picker.click(),
    });
    const refs = new Map();
    const cards = state.media.map((row, index) => {
      const n = index + 1;
      const image = h('img', {
        class: 'photo__image', src: row.url, alt: altOf(row, index), loading: 'lazy', decoding: 'async',
        width: row.width, height: row.height,
      });
      const toggle = switchControl({
        checked: row.is_published === true, label: t('gallery.publish'),
        hiddenLabel: t('gallery.publish_for', { n }),
        onChange: (checked) => publish(row.media_id, checked),
      });
      const earlier = iconButton('arrow-left', `${t('gallery.move_earlier')}: ${t('gallery.photo', { n })}`, {
        disabled: index === 0, onClick: () => reorder(index, -1),
      });
      const later = iconButton('arrow-right', `${t('gallery.move_later')}: ${t('gallery.photo', { n })}`, {
        disabled: index === state.media.length - 1, onClick: () => reorder(index, 1),
      });
      const del = iconButton('trash', `${t('common.delete')}: ${t('gallery.photo', { n })}`, {
        onClick: () => remove(row.media_id, del),
      });
      refs.set(row.media_id, { earlier, later, remove: del, switch: toggle.input });
      return h('li', { class: ['photo', !row.is_published && 'photo--hidden'] },
        h('div', { class: 'photo__frame' }, image,
          h('span', { class: 'photo__badge' },
            statusIcon(row.is_published ? 'good' : 'neutral', 14),
            t(row.is_published ? 'gallery.published' : 'gallery.hidden'))),
        h('div', { class: 'photo__body' },
          h('div', { class: 'photo__top' }, toggle.node,
            h('span', { class: 'photo__meta', text: t('gallery.meta', {
              width: row.width, height: row.height, size: bytes(row.byte_size),
            }) })),
          altField(row, 'ka', image, index), altField(row, 'en', image, index),
          h('div', { class: 'photo__actions' },
            h('div', { class: 'photo__moves' }, earlier, later), del)));
    });
    const pending = state.uploads.map((item) => item.node);
    mount(root,
      h('div', { class: 'toolbar toolbar--wrap' }, add, picker,
        h('p', { class: 'toolbar__hint', text: `${t('gallery.upload_hint')} ${t('gallery.alt_hint')}` })),
      cards.length || pending.length
        ? h('ul', { class: 'photos', 'aria-label': t('gallery.list') }, cards, pending)
        : card({}, emptyState({ iconName: 'image', title: t('gallery.empty') })));
    if (typing) {
      const input = [...root.querySelectorAll('[data-alt]')]
        .find((node) => node.dataset.alt === typing.key);
      if (input) {
        input.focus({ preventScroll: true });
        input.setSelectionRange(typing.start, typing.end);
      }
      return;
    }
    if (!focus) {
      return;
    }
    if (focus.add) {
      add.focus();
      return;
    }
    const ref = refs.get(focus.id);
    if (!ref) {
      return;
    }
    if (focus.part === 'earlier' || focus.part === 'later') {
      const wanted = focus.part === 'earlier' ? ref.earlier : ref.later;
      const other = focus.part === 'earlier' ? ref.later : ref.earlier;
      (wanted.disabled ? other : wanted).focus();
    } else if (ref[focus.part]) {
      ref[focus.part].focus();
    }
  }

  function draw() {
    if (state.status === 'loading') {
      mount(root, loadingBlock(h('div', { class: 'photos' },
        Array.from({ length: 3 }, () => h('div', { class: 'photo photo--skeleton' },
          h('div', { class: 'photo__frame' }, skeleton('image')),
          h('div', { class: 'photo__body' }, skeleton('line'), skeleton('input', 2)))))));
      return;
    }
    if (state.status === 'error') {
      mount(root, isForbidden(state.error) ? adminOnly() : card({}, errorState(state.error, load)));
      return;
    }
    drawGrid(null);
  }

  async function load() {
    state.status = 'loading';
    state.error = null;
    draw();
    try {
      const response = await api.media();
      if (!alive) {
        return;
      }
      state.media = response.media || [];
      state.status = 'ready';
    } catch (error) {
      if (!alive) {
        return;
      }
      state.error = error;
      state.status = 'error';
    }
    draw();
  }

  load();
  return {
    redraw: draw,
    destroy() {
      alive = false;
    },
  };
}
