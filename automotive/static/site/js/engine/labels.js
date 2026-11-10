/**
 * Pins and legend.
 *
 * The legend is server-rendered HTML (two languages), so nothing here knows a
 * label. A pin's number is its legend item's position among the items that are
 * actually displayed, which is also what the CSS counter in the legend shows.
 *
 * A pin is a callout: a small dot on the part, a short leader pointing away from
 * the middle of the picture, and the numbered tag at its end, so the tag never
 * sits on top of the part it names.
 */

const HOT = 'is-hot';

function element(tag, className) {
  const node = document.createElement(tag);
  node.className = className;
  return node;
}

export function createLabels(root) {
  const list = root.querySelector('[data-engine-pins]');
  const legend = root.querySelector('[data-engine-legend]');
  let pins = [];
  const detach = [];

  function clear() {
    while (detach.length) detach.pop()();
    pins = [];
    if (list) {
      list.replaceChildren();
      list.classList.remove('engine__pins--static', 'is-visible');
    }
  }

  /** (Re)build one pin per displayed legend item. Call again when the layout changes. */
  function sync() {
    clear();
    if (!list || !legend) return;
    let number = 0;
    for (const item of legend.querySelectorAll('li[data-part]')) {
      if (getComputedStyle(item).display === 'none') continue;
      number += 1;
      const pin = element('li', 'engine__pin is-off');
      pin.dataset.part = item.dataset.part;
      const line = element('i', 'engine__pin-line');
      const tag = element('span', 'engine__pin-tag');
      const dot = element('span', 'engine__pin-dot');
      dot.textContent = String(number);
      tag.append(dot);
      pin.append(line, tag);
      list.append(pin);
      pins.push({ key: item.dataset.part, pin, line, tag, x: NaN, y: NaN, angle: NaN, on: false });
      const heat = () => pin.classList.add(HOT);
      const cool = () => pin.classList.remove(HOT);
      for (const [type, handler] of [
        ['pointerenter', heat],
        ['pointerleave', cool],
        ['focusin', heat],
        ['focusout', cool],
      ]) {
        item.addEventListener(type, handler);
        detach.push(() => item.removeEventListener(type, handler));
      }
    }
  }

  /** Length of the leader in CSS pixels, for a given art box width. */
  function reach(width) {
    return width < 420 ? 20 : 30;
  }

  /**
   * Move pins over the live frame. `locate(key, out)` writes fractions of the
   * art box into `out` and returns whether the part is in frame. `centre` is the
   * middle of the composition, as fractions; leaders point away from it.
   */
  function track(locate, width, height, centre = [0.5, 0.5]) {
    const at = [0, 0, 0, 0];
    const length = reach(width);
    for (const entry of pins) {
      const on = locate(entry.key, at);
      if (on) {
        const x = Math.round(at[0] * width * 2) / 2;
        const y = Math.round(at[1] * height * 2) / 2;
        if (x !== entry.x || y !== entry.y) {
          entry.x = x;
          entry.y = y;
          entry.pin.style.transform = `translate3d(${x}px, ${y}px, 0)`;
          // The part may ask for a direction; otherwise point away from the middle.
          // Quantised, so the leader does not shimmer as the camera drifts.
          const raw =
            at[2] || at[3]
              ? Math.atan2(at[3], at[2])
              : Math.atan2(y - centre[1] * height, x - centre[0] * width);
          const angle = Math.round(raw * 24) / 24;
          if (angle !== entry.angle) {
            entry.angle = angle;
            const dx = (Math.cos(angle) * length).toFixed(1);
            const dy = (Math.sin(angle) * length).toFixed(1);
            entry.line.style.transform = `rotate(${angle.toFixed(4)}rad)`;
            entry.line.style.width = `${length}px`;
            entry.tag.style.transform = `translate(${dx}px, ${dy}px)`;
          }
        }
      }
      if (on !== entry.on) {
        entry.on = on;
        entry.pin.classList.toggle('is-off', !on);
      }
    }
  }

  /** Place pins from baked positions ({ key: [x, y] } as fractions of the art box). */
  function place(baked, width, height, centre) {
    track(
      (key, out) => {
        const at = baked && baked[key];
        if (!at) return false;
        out[0] = at[0];
        out[1] = at[1];
        out[2] = at[2] || 0;
        out[3] = at[3] || 0;
        return true;
      },
      width,
      height,
      centre,
    );
  }

  /**
   * Static tier: tags sit beside their part by percentage, with no transforms.
   * `all` is the whole of pins.json. The pin box overlays the exploded poster;
   * its shape says which poster that is, and how much of a landscape poster's
   * width is on display (CSS crops it to its right-hand part).
   */
  function pinStatic(all) {
    if (!list) return;
    list.classList.add('engine__pins--static', 'is-visible');
    const width = list.clientWidth || 1;
    const height = list.clientHeight || 1;
    const portrait = width / height < 0.9;
    const baked = portrait ? all.portrait : all.landscape;
    const shown = portrait ? 1 : Math.min(1, width / height / 1.6);
    const length = reach(width) * 0.7;
    for (const entry of pins) {
      const at = baked && baked[entry.key];
      const x = at ? (at[0] - (1 - shown)) / shown : -1;
      const on = Boolean(at) && x > 0.02 && x < 0.98;
      entry.pin.classList.toggle('is-off', !on);
      entry.pin.style.transform = '';
      if (!on) continue;
      const angle =
        at[2] || at[3] ? Math.atan2(at[3], at[2]) : Math.atan2((at[1] - 0.5) * height, (x - 0.5) * width);
      entry.pin.style.left = `${(x * 100 + (Math.cos(angle) * length * 100) / width).toFixed(2)}%`;
      entry.pin.style.top = `${(at[1] * 100 + (Math.sin(angle) * length * 100) / height).toFixed(2)}%`;
    }
  }

  function show(visible) {
    if (list) list.classList.toggle('is-visible', visible);
  }

  /**
   * Current pin positions for baking pins.json: [x, y] as fractions of the art
   * box, plus a unit leader direction when the part asks for one.
   */
  function snapshot(locate) {
    const out = {};
    const at = [0, 0, 0, 0];
    for (const entry of pins) {
      if (!locate(entry.key, at)) continue;
      const spot = [Number(at[0].toFixed(4)), Number(at[1].toFixed(4))];
      const size = Math.hypot(at[2], at[3]);
      if (size > 0) spot.push(Number((at[2] / size).toFixed(3)), Number((at[3] / size).toFixed(3)));
      out[entry.key] = spot;
    }
    return out;
  }

  sync();
  return { sync, track, place, pinStatic, show, snapshot, destroy: clear };
}
