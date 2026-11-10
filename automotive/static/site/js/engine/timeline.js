/**
 * Engine section timeline.
 *
 * Everything in this file is a pure function of scroll progress `p` in [0, 1]:
 * part poses, the camera, copy beats and crank rotation. Scrolling up therefore
 * reassembles the engine exactly. There is no DOM and no three.js here, so Node
 * can test it and a pre-rendered frame sequence could reuse the choreography.
 */

/** Phase boundaries of the scroll. 0 to explodeStart is the assembled hero frame. */
export const PHASE = Object.freeze({
  explodeStart: 0.1,
  explodeEnd: 0.68,
  holdEnd: 0.9,
});

/** Progress at which copy beat 1 and beat 2 begin. */
export const BEAT_STARTS = Object.freeze([0.22, 0.68]);

/** Progress at which the lite tier swaps the assembled poster for the exploded one. */
export const POSTER_SWAP = Object.freeze([0.4]);

/** Progress at which pins appear in the 3D tier (they need the parts to have settled). */
export const PINS_START = Object.freeze([0.66]);

export const HYSTERESIS = 0.02;

/** Crank revolutions over the whole scroll. Two turns is one four-stroke cycle. */
export const CRANK_TURNS = 2;

/**
 * Per-axis multipliers for explode distances: [lateral x, vertical y, axial z].
 * A phone has height to spare and no width, so the stack grows taller there.
 */
export const LAYOUT_SCALE = Object.freeze({
  landscape: Object.freeze([1, 1, 1]),
  portrait: Object.freeze([0.55, 1.3, 0.45]),
});

export const CAMERA = Object.freeze({
  fov: 28,
  azimuth: Object.freeze([40, 52]),
  elevation: Object.freeze([18, 9]),
  /** Fraction of the safe area kept free on every side. */
  margin: 0.05,
});

const DEG = Math.PI / 180;

export function clamp(value, low, high) {
  return value < low ? low : value > high ? high : value;
}

export function clamp01(value) {
  return value < 0 ? 0 : value > 1 ? 1 : value;
}

export function lerp(a, b, t) {
  return a + (b - a) * t;
}

export function smoothstep(edge0, edge1, value) {
  const t = clamp01((value - edge0) / (edge1 - edge0));
  return t * t * (3 - 2 * t);
}

/**
 * CSS-compatible cubic-bezier easing. Returns a function mapping x in [0, 1] to y.
 * Newton-Raphson first, bisection when the slope is too flat to trust.
 */
export function cubicBezier(x1, y1, x2, y2) {
  const cx = 3 * x1;
  const bx = 3 * (x2 - x1) - cx;
  const ax = 1 - cx - bx;
  const cy = 3 * y1;
  const by = 3 * (y2 - y1) - cy;
  const ay = 1 - cy - by;
  const sampleX = (t) => ((ax * t + bx) * t + cx) * t;
  const sampleY = (t) => ((ay * t + by) * t + cy) * t;
  const slopeX = (t) => (3 * ax * t + 2 * bx) * t + cx;

  return function ease(x) {
    if (x <= 0) return 0;
    if (x >= 1) return 1;
    let t = x;
    for (let i = 0; i < 8; i += 1) {
      const error = sampleX(t) - x;
      if (Math.abs(error) < 1e-7) return sampleY(t);
      const slope = slopeX(t);
      if (Math.abs(slope) < 1e-6) break;
      t -= error / slope;
    }
    let low = 0;
    let high = 1;
    t = x;
    for (let i = 0; i < 48; i += 1) {
      const value = sampleX(t);
      if (Math.abs(value - x) < 1e-7) break;
      if (value < x) low = t;
      else high = t;
      t = (low + high) / 2;
    }
    return sampleY(t);
  };
}

/** The site's --ease-in-out curve. Every part eases with it inside its own window. */
export const easeInOut = cubicBezier(0.77, 0, 0.175, 1);

/** Eased 0..1 travel of a part at progress p. Parts without a window never move. */
export function partAmount(part, p) {
  if (!part.window) return 0;
  const [start, end] = part.window;
  return easeInOut(clamp01((p - start) / (end - start)));
}

/**
 * Eased travel of one instance of a part whose instances leave one after another.
 * `part.stagger[index]` delays the start; all instances arrive when the window ends.
 */
export function instanceAmount(part, index, p) {
  if (!part.window) return 0;
  if (!part.stagger) return partAmount(part, p);
  const [start, end] = part.window;
  let longest = 0;
  for (const delay of part.stagger) longest = Math.max(longest, delay);
  const span = end - start - longest;
  return easeInOut(clamp01((p - start - part.stagger[index]) / span));
}

/** Whether a part travels in this layout. On phones the far bank stays put as an anchor. */
export function partMoves(part, layout) {
  return Boolean(part.window) && !(layout === 'portrait' && part.portrait !== 'moves');
}

const UNSCALED = Object.freeze([1, 1, 1]);

function layoutScale(part, layout) {
  // A rigid part must leave along its own axis (a piston along its bore).
  if (part.rigid) return UNSCALED;
  return LAYOUT_SCALE[layout] || LAYOUT_SCALE.landscape;
}

/**
 * A part's own travel (not counting what it rides on) once its window has
 * ended, in metres.
 */
export function partTravel(part, layout, out = [0, 0, 0]) {
  const scale = layoutScale(part, layout);
  const amount = partMoves(part, layout) ? 1 : 0;
  out[0] = part.explode[0] * scale[0] * amount + 0;
  out[1] = part.explode[1] * scale[1] * amount + 0;
  out[2] = part.explode[2] * scale[2] * amount + 0;
  return out;
}

/**
 * Displacement of a part from its assembled position, in metres. A part that
 * `rides` another one (a spark plug in its head) first pulls free in its own
 * window and is then carried along by the part it sits in.
 */
export function partOffset(part, p, layout, out = [0, 0, 0]) {
  const scale = layoutScale(part, layout);
  const amount = partMoves(part, layout) ? partAmount(part, p) : 0;
  let x = part.explode[0] * scale[0] * amount;
  let y = part.explode[1] * scale[1] * amount;
  let z = part.explode[2] * scale[2] * amount;
  if (part.rides) {
    partOffset(part.rides, p, layout, out);
    x += out[0];
    y += out[1];
    z += out[2];
  }
  // Adding zero turns a negative zero into a plain one.
  out[0] = x + 0;
  out[1] = y + 0;
  out[2] = z + 0;
  return out;
}

/** Crank angle in radians. Negative is clockwise seen from the front of the engine. */
export function crankAngle(p) {
  return 0 - p * CRANK_TURNS * 2 * Math.PI;
}

/** Distance from the crank axis to the piston pin, for a crank pin `theta` off the bore axis. */
export function pistonPinHeight(theta, strokeRadius, rodLength) {
  const side = Math.sin(theta) * strokeRadius;
  return Math.cos(theta) * strokeRadius + Math.sqrt(rodLength * rodLength - side * side);
}

/** Angle of the connecting rod away from the bore axis. */
export function rodSwing(theta, strokeRadius, rodLength) {
  return Math.asin((Math.sin(theta) * strokeRadius) / rodLength);
}

/** How firmly parts are still seated: 1 assembled, 0 once the explosion is under way. */
export function seatAmount(p) {
  return 1 - smoothstep(PHASE.explodeStart, 0.42, p);
}

/** Camera orbit angles in radians. The sweep runs gently across the whole scroll. */
export function cameraAngles(p, out = {}) {
  const t = (1 - Math.cos(Math.PI * clamp01(p))) / 2;
  out.azimuth = lerp(CAMERA.azimuth[0], CAMERA.azimuth[1], t) * DEG;
  out.elevation = lerp(CAMERA.elevation[0], CAMERA.elevation[1], t) * DEG;
  return out;
}

/** Orthonormal view basis: `k` points from the target to the camera, `r` right, `u` up. */
export function viewBasis(azimuth, elevation, out = {}) {
  const ca = Math.cos(azimuth);
  const sa = Math.sin(azimuth);
  const ce = Math.cos(elevation);
  const se = Math.sin(elevation);
  out.k = [ce * sa, se, ce * ca];
  out.r = [ca, 0, -sa];
  out.u = [-se * sa, ce, -se * ca];
  return out;
}

function centreOnAxis(points, count, axis, k, distance) {
  // Find the shift along `axis` that makes the projected extent symmetric.
  let low = Infinity;
  let high = -Infinity;
  for (let i = 0; i < count; i += 1) {
    const a = points[3 * i] * axis[0] + points[3 * i + 1] * axis[1] + points[3 * i + 2] * axis[2];
    if (a < low) low = a;
    if (a > high) high = a;
  }
  for (let step = 0; step < 24; step += 1) {
    const shift = (low + high) / 2;
    let right = -Infinity;
    let left = -Infinity;
    for (let i = 0; i < count; i += 1) {
      const x = points[3 * i];
      const y = points[3 * i + 1];
      const z = points[3 * i + 2];
      const a = x * axis[0] + y * axis[1] + z * axis[2] - shift;
      const depth = distance - (x * k[0] + y * k[1] + z * k[2]);
      const ndc = a / depth;
      if (ndc > right) right = ndc;
      if (-ndc > left) left = -ndc;
    }
    if (right > left) low = shift;
    else high = shift;
  }
  return (low + high) / 2;
}

/**
 * Smallest camera distance that keeps every point inside the frustum, and the
 * offsets along screen right and up that centre the point cloud in the frame.
 * `halfTanX` and `halfTanY` are the usable half-extents of the frame at unit depth.
 */
export function fitView(points, count, basis, halfTanX, halfTanY) {
  const { k, r, u } = basis;
  let right = -Infinity;
  let left = -Infinity;
  let top = -Infinity;
  let bottom = -Infinity;
  for (let i = 0; i < count; i += 1) {
    const x = points[3 * i];
    const y = points[3 * i + 1];
    const z = points[3 * i + 2];
    const a = (x * r[0] + y * r[1] + z * r[2]) / halfTanX;
    const b = (x * u[0] + y * u[1] + z * u[2]) / halfTanY;
    const c = x * k[0] + y * k[1] + z * k[2];
    if (c + a > right) right = c + a;
    if (c - a > left) left = c - a;
    if (c + b > top) top = c + b;
    if (c - b > bottom) bottom = c - b;
  }
  const distance = Math.max((right + left) / 2, (top + bottom) / 2);
  return {
    distance,
    shiftRight: centreOnAxis(points, count, r, k, distance),
    shiftUp: centreOnAxis(points, count, u, k, distance),
  };
}

function blur(values, stride, radius) {
  const count = values.length / stride;
  const out = new Float64Array(values.length);
  for (let i = 0; i < count; i += 1) {
    for (let c = 0; c < stride; c += 1) {
      let sum = 0;
      let weight = 0;
      for (let j = -radius; j <= radius; j += 1) {
        const index = clamp(i + j, 0, count - 1);
        const w = radius + 1 - Math.abs(j);
        sum += values[index * stride + c] * w;
        weight += w;
      }
      out[i * stride + c] = sum / weight;
    }
  }
  return out;
}

const FULL_FRAME = Object.freeze([0, 0, 1, 1]);

/**
 * The frame as seen from a camera whose axis runs through the centre of the
 * safe area rather than the centre of the picture (a lens shift, as on a view
 * camera). `safe` is [left, top, right, bottom] in fractions of the frame; the
 * model is framed inside it and the rest is left free for copy laid over the art.
 *
 * Returns the safe half-extents and the whole frame's bounds, all at unit depth.
 */
export function frameWindow(aspect, safe = FULL_FRAME) {
  const tanY = Math.tan((CAMERA.fov * DEG) / 2);
  const tanX = tanY * aspect;
  const centreX = tanX * (safe[0] + safe[2] - 1);
  const centreY = -tanY * (safe[1] + safe[3] - 1);
  return {
    halfX: tanX * (safe[2] - safe[0]),
    halfY: tanY * (safe[3] - safe[1]),
    left: -tanX - centreX,
    right: tanX - centreX,
    top: tanY - centreY,
    bottom: -tanY - centreY,
  };
}

/**
 * Sample the tight camera fit along the scroll and turn it into a smooth track.
 *
 * `samplePoints(p)` must return `{ points, count }`: the posed support points of
 * the model at progress p (a flat xyz array). The distance never decreases while
 * scrolling down and leads the explosion slightly, so no part pops out of frame.
 *
 * A generator, so the caller can spread the work: it yields after each sample
 * and returns the track.
 */
export function* cameraTrackSteps(samplePoints, aspect, options = {}) {
  const steps = options.steps ?? 32;
  const margin = options.margin ?? CAMERA.margin;
  const lead = options.lead ?? 2;
  const window = frameWindow(aspect, options.safe);
  const halfTanY = window.halfY * (1 - 2 * margin);
  const halfTanX = window.halfX * (1 - 2 * margin);
  const distance = new Float64Array(steps + 1);
  const target = new Float64Array((steps + 1) * 3);
  const angles = {};
  const basis = {};
  for (let i = 0; i <= steps; i += 1) {
    const p = i / steps;
    cameraAngles(p, angles);
    viewBasis(angles.azimuth, angles.elevation, basis);
    const { points, count } = samplePoints(p);
    const fit = fitView(points, count, basis, halfTanX, halfTanY);
    distance[i] = fit.distance;
    for (let c = 0; c < 3; c += 1) {
      target[3 * i + c] = fit.shiftRight * basis.r[c] + fit.shiftUp * basis.u[c];
    }
    yield;
  }
  const leading = new Float64Array(steps + 1);
  let running = 0;
  for (let i = 0; i <= steps; i += 1) {
    let peak = running;
    for (let j = i; j <= Math.min(steps, i + lead); j += 1) peak = Math.max(peak, distance[j]);
    running = Math.max(running, distance[i]);
    leading[i] = peak;
  }
  return {
    steps,
    aspect,
    window,
    distance: blur(leading, 1, 2),
    target: blur(target, 3, 2),
    tight: distance,
  };
}

/** `cameraTrackSteps` run to its end in one go. */
export function buildCameraTrack(samplePoints, aspect, options = {}) {
  const steps = cameraTrackSteps(samplePoints, aspect, options);
  let step = steps.next();
  while (!step.done) step = steps.next();
  return step.value;
}

function catmull(values, stride, offset, position) {
  const last = values.length / stride - 1;
  const index = clamp(Math.floor(position), 0, last);
  const t = clamp01(position - index);
  const at = (i) => values[clamp(i, 0, last) * stride + offset];
  const p0 = at(index - 1);
  const p1 = at(index);
  const p2 = at(index + 1);
  const p3 = at(index + 2);
  return (
    0.5 *
    (2 * p1 +
      (p2 - p0) * t +
      (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t +
      (3 * p1 - p0 - 3 * p2 + p3) * t * t * t)
  );
}

/** Camera position and look-at target at progress p, from a track built above. */
export function cameraAt(track, p, out = {}) {
  const angles = cameraAngles(p);
  const basis = viewBasis(angles.azimuth, angles.elevation);
  const position = clamp01(p) * track.steps;
  const distance = catmull(track.distance, 1, 0, position);
  out.target = out.target || [0, 0, 0];
  out.position = out.position || [0, 0, 0];
  for (let c = 0; c < 3; c += 1) {
    out.target[c] = catmull(track.target, 3, c, position);
    out.position[c] = out.target[c] + basis.k[c] * distance;
  }
  out.distance = distance;
  out.azimuth = angles.azimuth;
  out.elevation = angles.elevation;
  return out;
}

/**
 * Index of the step `p` falls in, given ascending thresholds, with hysteresis so a
 * finger resting on a boundary cannot make the state flicker. Pass the previous
 * index, or -1 when there is none yet.
 */
export function stepAt(p, thresholds, previous = -1, hysteresis = HYSTERESIS) {
  if (previous < 0 || previous > thresholds.length) {
    let index = 0;
    while (index < thresholds.length && p >= thresholds[index]) index += 1;
    return index;
  }
  let index = previous;
  while (index < thresholds.length && p >= thresholds[index] + hysteresis) index += 1;
  while (index > 0 && p < thresholds[index - 1] - hysteresis) index -= 1;
  return index;
}

/** Copy beat (0, 1 or 2) for progress p. */
export function beatAt(p, previous = -1) {
  return stepAt(p, BEAT_STARTS, previous);
}

/** Poster index (0 assembled, 1 exploded) for the lite tier. */
export function posterAt(p, previous = -1) {
  return stepAt(p, POSTER_SWAP, previous);
}

/** 1 while pins should be shown over the live 3D frame, else 0. */
export function pinsAt(p, previous = -1) {
  return stepAt(p, PINS_START, previous);
}

/**
 * Scroll progress of a tall section with a sticky stage.
 * `top` is the section's viewport top, `stickyTop` where the stage sticks.
 */
export function scrollProgress(top, sectionHeight, stageHeight, stickyTop) {
  const travel = sectionHeight - stageHeight;
  if (!(travel > 0)) return 0;
  return clamp01((stickyTop - top) / travel);
}

/**
 * One step of progress smoothing: exponential approach with a time constant, a
 * hard limit on how far the picture may lag the finger, and a snap when close.
 */
export function smoothProgress(current, target, elapsedMs, timeConstant = 90, maxLag = 0.06) {
  const blend = 1 - Math.exp(-Math.max(0, elapsedMs) / timeConstant);
  let next = current + (target - current) * blend;
  if (target - next > maxLag) next = target - maxLag;
  else if (next - target > maxLag) next = target + maxLag;
  if (Math.abs(target - next) < 0.0005) next = target;
  return next;
}
