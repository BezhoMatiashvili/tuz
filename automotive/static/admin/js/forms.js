// Form pieces shared by the text, service and gallery editors: inputs with a live counter
// that counts the way the server does, Georgian/English pairs, and the phone language tabs.

import { h, uid } from './dom.js';
import { t } from './strings.js';
import { field, textArea, textInput } from './ui.js';

// The server drops control characters, collapses runs of white space and trims before it
// measures a text. Counting the same way keeps the counter and the server in agreement.
// Line and paragraph separators (U+2028, U+2029) are written as code points on purpose:
// typed literally they would end the line inside a regular expression.
const SEPARATORS = String.fromCharCode(0x2028, 0x2029);
const CONTROL = new RegExp(`[\\u0000-\\u0008\\u000b\\u000c\\u000e-\\u001f\\u007f${SEPARATORS}]`, 'g');

export function normalize(value, multiline = false) {
  const text = String(value || '').replace(CONTROL, '');
  if (multiline) {
    return text.split(/\r\n|\r|\n/).map((line) => line.split(/\s+/).filter(Boolean).join(' '))
      .join('\n').trim();
  }
  return text.split(/\s+/).filter(Boolean).join(' ');
}

/** Length in code points, like Python's len(). */
export function countChars(value, multiline = false) {
  return [...normalize(value, multiline)].length;
}

export function countWords(value, multiline = false) {
  return normalize(value, multiline).split(/\s+/).filter(Boolean).length;
}

/** The kind of value a text field holds, guessed from its name; picks the phone keyboard. */
export function inputKind(name) {
  if (name === 'phone' || name === 'whatsapp') {
    return 'tel';
  }
  if (name === 'email') {
    return 'email';
  }
  if (name === 'map_url' || name.endsWith('_url') || ['facebook', 'instagram', 'tiktok'].includes(name)) {
    return 'url';
  }
  return 'text';
}

/** The sentence for a server field code, more specific where the input kind is known. */
export function fieldMessage(code, kind = 'text') {
  if (!code) {
    return '';
  }
  if (code === 'INVALID' && kind === 'tel') {
    return t('field.invalid.phone');
  }
  if (code === 'INVALID' && kind === 'url') {
    return t('field.invalid.url');
  }
  if (code === 'INVALID' && kind === 'email') {
    return t('field.invalid.email');
  }
  if (code === 'INVALID' && kind === 'username') {
    return t('field.invalid.username');
  }
  if (code === 'INVALID' && kind === 'price') {
    return t('field.invalid.price');
  }
  if (code === 'TOO_LONG' && kind === 'password') {
    return t('field.password.TOO_LONG');
  }
  const key = `field.${code}`;
  const text = t(key);
  return text === key ? t('field.INVALID') : text;
}

/** Let a textarea take the height of its text, also after it becomes visible or wider. */
function autoGrow(area) {
  let width = -1;
  const fit = () => {
    if (!area.isConnected || area.clientWidth === 0) {
      return;
    }
    area.style.height = 'auto';
    area.style.height = `${area.scrollHeight + 2}px`;
  };
  area.addEventListener('input', fit);
  new ResizeObserver(() => {
    if (area.clientWidth !== width) {
      width = area.clientWidth;
      fit();
    }
  }).observe(area);
}

/**
 * A text input or textarea with its label above and a live counter against `max`
 * (and against `words` when set). Typing never stops at the limit: the counter turns
 * to its warning state and the server has the final word when saving.
 * Returns the field controller plus `kind`.
 */
export function limitedInput({
  label, badge, value, max, words = 0, multiline = false, kind = 'text', hint, lang, onInput,
  placeholder, required,
}) {
  const props = {
    value: value || '', lang: lang || null, placeholder: placeholder || null,
    'aria-required': required ? 'true' : null,
  };
  // Longer one-line texts get a growing box too, so the whole text stays in view on a
  // phone. A line break would be thrown away by the server, so Enter does nothing there.
  const area = multiline || (kind === 'text' && max > 80);
  let control;
  if (area) {
    control = textArea({ ...props, rows: multiline ? 3 : 2, class: `input input--area${multiline ? '' : ' input--area-short'}` });
    autoGrow(control);
    if (!multiline) {
      control.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
        }
      });
    }
  } else {
    control = textInput({
      ...props, type: kind === 'price' || kind === 'username' ? 'text' : kind,
      inputmode: kind === 'tel' ? 'tel' : kind === 'url' ? 'url' : kind === 'email' ? 'email'
        : kind === 'price' ? 'decimal' : null,
      autocapitalize: kind === 'text' ? null : 'none',
      spellcheck: kind === 'text' ? null : 'false',
    });
  }
  const controller = field({ label, badge, control, hint, counter: Boolean(max) });
  const refresh = () => {
    if (!max) {
      return;
    }
    const count = countChars(control.value, multiline);
    let text = t('common.chars', { count, max });
    let over = count > max;
    if (words) {
      const used = countWords(control.value, multiline);
      text = `${text} · ${t('common.words', { count: used, max: words })}`;
      over = over || used > words;
    }
    controller.setCounter(text, over, t('common.chars_label', { count, max }));
  };
  control.addEventListener('input', () => {
    refresh();
    if (onInput) {
      onInput(control.value);
    }
  });
  refresh();
  return { ...controller, kind };
}

/**
 * Georgian and English inputs for one field: side by side on wide screens, one at a time
 * on phones (the form's language tabs choose which). `make(lang)` builds one side.
 */
export function langPair(make) {
  return h('div', { class: 'pair' },
    ['ka', 'en'].map((lang) => h('div', { class: 'pair__side', dataset: { lang } }, make(lang))));
}

/**
 * The KA / EN tabs shown on phones. Sets data-lang on `form`, which decides which side of
 * every pair is visible. Returns {node, show(lang), mark(langs)} where `mark` flags the
 * languages that currently hold an error.
 */
export function langTabs(form, initial = 'ka', onChange) {
  const name = uid('lang');
  const markers = {};
  const inputs = {};
  form.dataset.lang = initial;
  const node = h('div', { class: 'segmented lang-tabs', role: 'radiogroup', 'aria-label': t('texts.language') },
    ['ka', 'en'].map((lang) => {
      const id = uid('lang');
      const input = h('input', {
        type: 'radio', class: 'segmented__input', name, id, value: lang, checked: lang === initial,
      });
      input.addEventListener('change', () => {
        if (input.checked) {
          form.dataset.lang = lang;
          if (onChange) {
            onChange(lang);
          }
        }
      });
      inputs[lang] = input;
      markers[lang] = h('span', { class: 'segmented__marker', hidden: true },
        h('span', { class: 'visually-hidden', text: `${t('texts.error_in')}. ` }));
      return h('label', { class: 'segmented__option', for: id }, input,
        h('span', { class: 'segmented__text' }, markers[lang],
          t(lang === 'ka' ? 'common.georgian' : 'common.english')));
    }));
  return {
    node,
    show(lang) {
      if (inputs[lang]) {
        inputs[lang].checked = true;
        form.dataset.lang = lang;
        if (onChange) {
          onChange(lang);
        }
      }
    },
    mark(langs) {
      for (const lang of ['ka', 'en']) {
        markers[lang].hidden = !langs.includes(lang);
      }
    },
  };
}
