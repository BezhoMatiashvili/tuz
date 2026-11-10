/**
 * Procedural geometry for the engine: a classic pushrod V8 in small-block
 * proportions. Every builder returns bevelled, merged geometry per material.
 *
 * Units are metres; X viewer-right, Y up, Z towards the viewer (engine front).
 * Bank parts are modelled in bank coordinates (u outboard across the deck,
 * v along the bore axis, w fore-aft) and then placed on the near or far bank.
 *
 * Vertex colours are data, not colour: red is tint times the part's own
 * occlusion, green is the occlusion cast by neighbouring parts (see materials.js).
 */

import {
  BoxGeometry,
  BufferAttribute,
  BufferGeometry,
  CatmullRomCurve3,
  ExtrudeGeometry,
  LatheGeometry,
  Matrix4,
  Path,
  Quaternion,
  Shape,
  TubeGeometry,
  Vector2,
  Vector3,
} from '../../vendor/three@0.187.0/three.module.js';
import { mergeGeometries } from '../../vendor/three@0.187.0/BufferGeometryUtils.js';
import { RoundedBoxGeometry } from '../../vendor/three@0.187.0/RoundedBoxGeometry.js';
import { BANKS, DIM, PIN_PHASE } from './parts.js';
import { instanceAmount, partTravel, pistonPinHeight, rodSwing } from './timeline.js';

const TAU = Math.PI * 2;
const DEG = Math.PI / 180;
const N = Math.SQRT1_2;

/** Seam between mating parts. */
const GAP = 0.0012;
const DECK = DIM.deckHeight;
const PITCH = DIM.boreSpacing;
const ZF = DIM.blockHalfLength;
const BORE_R = DIM.bore / 2;
/** Cylinder positions along a bank, front to rear, in bank coordinates. */
const W = [1.5, 0.5, -0.5, -1.5].map((k) => k * PITCH);

const V0 = DECK + 0.0019; // underside of the head: deck, gasket, seams
const HEAD_TOP = V0 + 0.0815;
const RAIL_TOP = HEAD_TOP + 0.011;
const VC0 = RAIL_TOP + GAP; // underside of the valve cover
const VC_TOP = VC0 + 0.05;
const COVER = { u0: -0.066, u1: 0.06, half: 0.231, mid: -0.003 };
const PLUG_AXIS = [Math.cos(DIM.plugTilt), Math.sin(DIM.plugTilt), 0];
const CAM_Y = DIM.camHeight;
const PUMP_Y = 0.178;
const ALT = [-0.205, 0.318];
const BELT_Z = 0.385;
const PULLEY = { crank: 0.0775, pump: 0.0725, alt: 0.0295 };
const DIST_Z = -0.232;
const CHAIN_Z = ZF + 0.011;
const SPROCKET = { crank: 0.0334, cam: 0.0667 };

const _vector = new Vector3();
const _origin = new Vector3();
const _quaternion = new Quaternion();
const _matrix = new Matrix4();
const _one = new Vector3(1, 1, 1);
const _up = new Vector3(0, 1, 0);

const clamp01 = (value) => (value < 0 ? 0 : value > 1 ? 1 : value);
const mix = (a, b, t) => a + (b - a) * t;
const smooth = (edge0, edge1, value) => {
  const t = clamp01((value - edge0) / (edge1 - edge0));
  return t * t * (3 - 2 * t);
};

/** Circumference detail: 1 on desktop, lower on phones. Set by `buildPart`. */
let detail = 1;

function around(segments) {
  if (segments <= 16) return segments;
  return Math.max(16, Math.round((segments * detail) / 4) * 4);
}

// ----------------------------------------------------------------------------
// Normals, colours and mesh utilities
// ----------------------------------------------------------------------------

const CREASE = Math.cos(50 * DEG);
const HARD = Math.cos(12 * DEG);
const QUANTUM = 20000; // 0.05 mm position grid for welding normals
const Q_BIAS = 65536;
const Q_SPAN = 131072;

/**
 * Recompute normals with a crease angle, weighting each face by its area and
 * corner angle.
 *
 * This does the job of three's `toCreasedNormals`, with one difference that
 * matters here: a large flat face outweighs the small bevel facet next to it.
 * Flats therefore stay flat and the whole highlight gradient lives on the
 * bevel, so even a single chamfer segment shades like a rounded edge. Also
 * drops degenerate triangles (lathe poles).
 *
 * Returns non-indexed geometry with position, normal and a white colour.
 */
function crease(source, limit = CREASE) {
  const flat = source.index ? source.toNonIndexed() : source;
  const position = flat.attributes.position.array;
  const corners = position.length / 3;
  const triangles = corners / 3;
  const faceNormal = new Float32Array(triangles * 3);
  const weight = new Float32Array(corners);
  const key = new Float64Array(corners);
  let alive = 0;
  for (let f = 0; f < triangles; f += 1) {
    const o = f * 9;
    const e1x = position[o + 3] - position[o];
    const e1y = position[o + 4] - position[o + 1];
    const e1z = position[o + 5] - position[o + 2];
    const e2x = position[o + 6] - position[o];
    const e2y = position[o + 7] - position[o + 1];
    const e2z = position[o + 8] - position[o + 2];
    const nx = e1y * e2z - e1z * e2y;
    const ny = e1z * e2x - e1x * e2z;
    const nz = e1x * e2y - e1y * e2x;
    const twice = Math.hypot(nx, ny, nz);
    if (twice < 1e-11) continue;
    alive += 1;
    faceNormal[f * 3] = nx / twice;
    faceNormal[f * 3 + 1] = ny / twice;
    faceNormal[f * 3 + 2] = nz / twice;
    const e3x = e2x - e1x;
    const e3y = e2y - e1y;
    const e3z = e2z - e1z;
    const l1 = Math.hypot(e1x, e1y, e1z);
    const l2 = Math.hypot(e2x, e2y, e2z);
    const l3 = Math.hypot(e3x, e3y, e3z);
    const cosA = (e1x * e2x + e1y * e2y + e1z * e2z) / (l1 * l2);
    const cosB = -(e1x * e3x + e1y * e3y + e1z * e3z) / (l1 * l3);
    const angleA = Math.acos(cosA < -1 ? -1 : cosA > 1 ? 1 : cosA);
    const angleB = Math.acos(cosB < -1 ? -1 : cosB > 1 ? 1 : cosB);
    weight[f * 3] = twice * Math.max(angleA, 1e-4);
    weight[f * 3 + 1] = twice * Math.max(angleB, 1e-4);
    weight[f * 3 + 2] = twice * Math.max(Math.PI - angleA - angleB, 1e-4);
  }
  const head = new Map();
  const next = new Int32Array(corners).fill(-1);
  for (let v = 0; v < corners; v += 1) {
    if (weight[v] === 0) continue;
    const qx = Math.round(position[v * 3] * QUANTUM) + Q_BIAS;
    const qy = Math.round(position[v * 3 + 1] * QUANTUM) + Q_BIAS;
    const qz = Math.round(position[v * 3 + 2] * QUANTUM) + Q_BIAS;
    const k = (qx * Q_SPAN + qy) * Q_SPAN + qz;
    key[v] = k;
    const previous = head.get(k);
    if (previous !== undefined) next[v] = previous;
    head.set(k, v);
  }
  const outPosition = new Float32Array(alive * 9);
  const outNormal = new Float32Array(alive * 9);
  let write = 0;
  for (let f = 0; f < triangles; f += 1) {
    if (weight[f * 3] === 0) continue;
    const fx = faceNormal[f * 3];
    const fy = faceNormal[f * 3 + 1];
    const fz = faceNormal[f * 3 + 2];
    for (let c = 0; c < 3; c += 1) {
      const v = f * 3 + c;
      let sx = 0;
      let sy = 0;
      let sz = 0;
      for (let other = head.get(key[v]); other !== -1; other = next[other]) {
        const g = Math.floor(other / 3) * 3;
        const gx = faceNormal[g];
        const gy = faceNormal[g + 1];
        const gz = faceNormal[g + 2];
        if (gx * fx + gy * fy + gz * fz < limit) continue;
        const share = weight[other];
        sx += gx * share;
        sy += gy * share;
        sz += gz * share;
      }
      const length = Math.hypot(sx, sy, sz) || 1;
      outPosition[write] = position[v * 3];
      outPosition[write + 1] = position[v * 3 + 1];
      outPosition[write + 2] = position[v * 3 + 2];
      outNormal[write] = sx / length;
      outNormal[write + 1] = sy / length;
      outNormal[write + 2] = sz / length;
      write += 3;
    }
  }
  return fromArrays(outPosition, outNormal);
}

function fromArrays(position, normal, colour) {
  const geometry = new BufferGeometry();
  geometry.setAttribute('position', new BufferAttribute(position, 3));
  geometry.setAttribute('normal', new BufferAttribute(normal, 3));
  geometry.setAttribute(
    'color',
    new BufferAttribute(colour || new Float32Array(position.length).fill(1), 3),
  );
  return geometry;
}

/** Keep a primitive's own (analytic) normals; strip everything else. */
function finish(geometry) {
  const flat = geometry.index ? geometry.toNonIndexed() : geometry;
  return fromArrays(flat.attributes.position.array, flat.attributes.normal.array);
}

/** Multiply the tint of a whole primitive. */
function paint(geometry, tint) {
  const colour = geometry.attributes.color.array;
  for (let i = 0; i < colour.length; i += 3) colour[i] *= tint;
  return geometry;
}

/** Multiply the tint per vertex: `fn(x, y, z, nx, ny, nz)` returns a factor. */
function shade(geometry, fn) {
  const position = geometry.attributes.position.array;
  const normal = geometry.attributes.normal.array;
  const colour = geometry.attributes.color.array;
  for (let i = 0; i < colour.length; i += 3) {
    colour[i] *= fn(
      position[i],
      position[i + 1],
      position[i + 2],
      normal[i],
      normal[i + 1],
      normal[i + 2],
    );
  }
  return geometry;
}

function swapCorners(array) {
  for (let i = 0; i < array.length; i += 9) {
    for (let c = 0; c < 3; c += 1) {
      const held = array[i + 3 + c];
      array[i + 3 + c] = array[i + 6 + c];
      array[i + 6 + c] = held;
    }
  }
}

/** Reverse triangle winding (after a mirror), optionally turning the surface inside out. */
function flip(geometry, insideOut = false) {
  swapCorners(geometry.attributes.position.array);
  swapCorners(geometry.attributes.normal.array);
  swapCorners(geometry.attributes.color.array);
  if (insideOut) {
    const normal = geometry.attributes.normal.array;
    for (let i = 0; i < normal.length; i += 1) normal[i] = -normal[i];
  }
  return geometry;
}

/** Remove triangles whose centroid and normal match `predicate`. */
function dropFaces(geometry, predicate) {
  const position = geometry.attributes.position.array;
  const normal = geometry.attributes.normal.array;
  const colour = geometry.attributes.color.array;
  const keep = [];
  for (let i = 0; i < position.length; i += 9) {
    const cx = (position[i] + position[i + 3] + position[i + 6]) / 3;
    const cy = (position[i + 1] + position[i + 4] + position[i + 7]) / 3;
    const cz = (position[i + 2] + position[i + 5] + position[i + 8]) / 3;
    const nx = (normal[i] + normal[i + 3] + normal[i + 6]) / 3;
    const ny = (normal[i + 1] + normal[i + 4] + normal[i + 7]) / 3;
    const nz = (normal[i + 2] + normal[i + 5] + normal[i + 8]) / 3;
    if (!predicate(cx, cy, cz, nx, ny, nz)) keep.push(i);
  }
  const take = (array) => {
    const out = new Float32Array(keep.length * 9);
    keep.forEach((start, index) => out.set(array.subarray(start, start + 9), index * 9));
    return out;
  };
  return fromArrays(take(position), take(normal), take(colour));
}

function move(geometry, x, y, z) {
  return geometry.translate(x, y, z);
}

/** Rotate +Y onto `direction`, then translate to `origin`. */
function place(geometry, origin, direction) {
  _vector.set(direction[0], direction[1], direction[2]).normalize();
  _quaternion.setFromUnitVectors(_up, _vector);
  _matrix.compose(_origin.set(origin[0], origin[1], origin[2]), _quaternion, _one);
  return geometry.applyMatrix4(_matrix);
}

/** Turn a solid of revolution from the Y axis onto the Z axis. */
function alongZ(geometry) {
  return geometry.rotateX(Math.PI / 2);
}

function bankMatrix(side) {
  const { n, t, shift } = BANKS[side];
  return new Matrix4().set(
    t[0], n[0], 0, 0,
    t[1], n[1], 0, 0,
    0, 0, 1, shift,
    0, 0, 0, 1,
  );
}

/** Bank coordinates to world coordinates. */
function bankPoint(side, u, v, w = 0) {
  const bank = BANKS[side];
  return [bank.sign * N * (v + u), N * (v - u), w + bank.shift];
}

/** Near-bank world coordinates to bank coordinates. */
function toBank(point) {
  return [N * (point[0] - point[1]), N * (point[0] + point[1]), point[2] - BANKS.near.shift];
}

/** Geometry authored in near-bank world space, moved into bank coordinates. */
function intoBank(geometry) {
  const { n, t, shift } = BANKS.near;
  _matrix.set(
    t[0], t[1], 0, 0,
    n[0], n[1], 0, 0,
    0, 0, 1, -shift,
    0, 0, 0, 1,
  );
  return geometry.applyMatrix4(_matrix);
}

function transform(geometry, matrix) {
  geometry.applyMatrix4(matrix);
  if (matrix.determinant() < 0) flip(geometry);
  return geometry;
}

/** Geometry collected per material for one part. */
class Bag {
  constructor() {
    this.lists = new Map();
  }

  add(material, geometry, tint = 1) {
    if (tint !== 1) paint(geometry, tint);
    let list = this.lists.get(material);
    if (!list) {
      list = [];
      this.lists.set(material, list);
    }
    list.push(geometry);
    return geometry;
  }

  take(other) {
    for (const [material, list] of other.lists) {
      for (const geometry of list) this.add(material, geometry);
    }
  }

  apply(matrix) {
    for (const list of this.lists.values()) {
      for (const geometry of list) transform(geometry, matrix);
    }
    return this;
  }
}

// ----------------------------------------------------------------------------
// 2D outlines
// ----------------------------------------------------------------------------

/**
 * Round the corners of a polyline. Points are [x, y] or [x, y, radius]; a radius
 * of 0 keeps the corner sharp. Small fillets get a single chamfer segment (the
 * weighted normals shade it round); larger ones get more.
 */
function roundPoly(points, radius = 0.002, closed = true, perQuarter = 0) {
  const count = points.length;
  const out = [];
  for (let i = 0; i < count; i += 1) {
    const p = points[i];
    const r = p[2] ?? radius;
    const isEnd = !closed && (i === 0 || i === count - 1);
    if (isEnd || r <= 0) {
      out.push([p[0], p[1]]);
      continue;
    }
    const a = points[(i - 1 + count) % count];
    const b = points[(i + 1) % count];
    let ux = a[0] - p[0];
    let uy = a[1] - p[1];
    let vx = b[0] - p[0];
    let vy = b[1] - p[1];
    const lu = Math.hypot(ux, uy);
    const lv = Math.hypot(vx, vy);
    if (lu < 1e-9 || lv < 1e-9) {
      out.push([p[0], p[1]]);
      continue;
    }
    ux /= lu;
    uy /= lu;
    vx /= lv;
    vy /= lv;
    const theta = Math.acos(Math.max(-1, Math.min(1, ux * vx + uy * vy)));
    if (theta > Math.PI - 0.03) {
      out.push([p[0], p[1]]);
      continue;
    }
    let reach = r / Math.tan(theta / 2);
    let fillet = r;
    const limit = 0.48 * Math.min(lu, lv);
    if (reach > limit) {
      reach = limit;
      fillet = reach * Math.tan(theta / 2);
    }
    const t1 = [p[0] + ux * reach, p[1] + uy * reach];
    const t2 = [p[0] + vx * reach, p[1] + vy * reach];
    let bx = ux + vx;
    let by = uy + vy;
    const bl = Math.hypot(bx, by);
    bx /= bl;
    by /= bl;
    const offset = fillet / Math.sin(theta / 2);
    const centre = [p[0] + bx * offset, p[1] + by * offset];
    const a1 = Math.atan2(t1[1] - centre[1], t1[0] - centre[0]);
    let sweep = Math.atan2(t2[1] - centre[1], t2[0] - centre[0]) - a1;
    while (sweep > Math.PI) sweep -= TAU;
    while (sweep < -Math.PI) sweep += TAU;
    const density = perQuarter || (fillet < 0.0035 ? 1 : fillet < 0.012 ? 2 : 3);
    const steps = Math.max(1, Math.ceil((Math.abs(sweep) / (Math.PI / 2)) * density - 0.01));
    for (let s = 0; s <= steps; s += 1) {
      const angle = a1 + (sweep * s) / steps;
      out.push([centre[0] + fillet * Math.cos(angle), centre[1] + fillet * Math.sin(angle)]);
    }
  }
  return out;
}

function roundRect(x0, y0, x1, y1, radius, perQuarter = 0) {
  return roundPoly([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], radius, true, perQuarter);
}

function circle(cx, cy, radius, segments, start = 0, end = TAU) {
  const full = Math.abs(end - start - TAU) < 1e-9;
  const steps = full ? segments : segments + 1;
  const out = [];
  for (let i = 0; i < steps; i += 1) {
    const angle = start + ((end - start) * i) / segments;
    out.push([cx + radius * Math.cos(angle), cy + radius * Math.sin(angle)]);
  }
  return out;
}

/** A gear or sprocket outline with flat-topped teeth. */
function gearOutline(teeth, root, tip, land = 0.3) {
  const out = [];
  const flank = (1 - 2 * land) / 2;
  for (let i = 0; i < teeth; i += 1) {
    for (const [fraction, radius] of [
      [0, root],
      [land, root],
      [land + flank, tip],
      [land + flank + land, tip],
    ]) {
      const angle = ((i + fraction) / teeth) * TAU;
      out.push([radius * Math.cos(angle), radius * Math.sin(angle)]);
    }
  }
  return out;
}

/**
 * The path of a belt or chain around circles listed counter-clockwise.
 * `at(s)` gives the point and travel direction at arc length s.
 */
function tangentLoop(circles) {
  const count = circles.length;
  const normals = circles.map((a, i) => {
    const b = circles[(i + 1) % count];
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    return Math.atan2(dy, dx) - Math.acos((a.r - b.r) / Math.hypot(dx, dy));
  });
  const pieces = [];
  let total = 0;
  circles.forEach((c, i) => {
    const from = normals[(i - 1 + count) % count];
    let to = normals[i];
    while (to < from) to += TAU;
    while (to - from >= TAU) to -= TAU;
    pieces.push({ arc: true, c, from, to, start: total, length: (to - from) * c.r });
    total += (to - from) * c.r;
    const b = circles[(i + 1) % count];
    const m = normals[i];
    const p0 = [c.x + c.r * Math.cos(m), c.y + c.r * Math.sin(m)];
    const p1 = [b.x + b.r * Math.cos(m), b.y + b.r * Math.sin(m)];
    const length = Math.hypot(p1[0] - p0[0], p1[1] - p0[1]);
    pieces.push({ arc: false, p0, p1, start: total, length });
    total += length;
  });
  return {
    length: total,
    at(distance, out = [0, 0, 0]) {
      let s = distance % total;
      if (s < 0) s += total;
      let piece = pieces[pieces.length - 1];
      for (const candidate of pieces) {
        if (s < candidate.start + candidate.length) {
          piece = candidate;
          break;
        }
      }
      const local = s - piece.start;
      if (piece.arc) {
        const angle = piece.from + local / piece.c.r;
        out[0] = piece.c.x + piece.c.r * Math.cos(angle);
        out[1] = piece.c.y + piece.c.r * Math.sin(angle);
        out[2] = angle + Math.PI / 2;
      } else {
        const t = piece.length > 0 ? local / piece.length : 0;
        out[0] = mix(piece.p0[0], piece.p1[0], t);
        out[1] = mix(piece.p0[1], piece.p1[1], t);
        out[2] = Math.atan2(piece.p1[1] - piece.p0[1], piece.p1[0] - piece.p0[0]);
      }
      return out;
    },
    outline(maxAngle = 9 * DEG) {
      const out = [];
      for (const piece of pieces) {
        if (!piece.arc) continue;
        const steps = Math.max(1, Math.ceil((piece.to - piece.from) / maxAngle));
        for (let i = 0; i <= steps; i += 1) {
          const angle = piece.from + ((piece.to - piece.from) * i) / steps;
          out.push([piece.c.x + piece.c.r * Math.cos(angle), piece.c.y + piece.c.r * Math.sin(angle)]);
        }
      }
      return out;
    },
  };
}

// ----------------------------------------------------------------------------
// Primitives. All return non-indexed geometry with position, normal, colour.
// ----------------------------------------------------------------------------

/**
 * Solid of revolution about +Y. `profile` is [radius, y] points listed so the
 * solid is on the left of the direction of travel (up the outside, inwards
 * across the top): that is what makes the faces point outwards.
 */
function lathe(profile, segments = 32, limit = CREASE) {
  const points = profile.map((p) => new Vector2(Math.max(p[0], 0), p[1]));
  return crease(new LatheGeometry(points, around(segments)), limit);
}

/** A flat disc facing +Y. */
function disc(radius, segments = 16) {
  return lathe([[radius, 0], [0, 0]], segments);
}

/** A flat ring facing +Y. */
function ring(inner, outer, segments = 32) {
  return lathe([[outer, 0], [inner, 0]], segments);
}

function toShape(points, holes = []) {
  const shape = new Shape(points.map((p) => new Vector2(p[0], p[1])));
  for (const hole of holes) shape.holes.push(new Path(hole.map((p) => new Vector2(p[0], p[1]))));
  return shape;
}

/**
 * Extrude an XY outline along +Z from 0 to `length`, with a quarter-round bevel
 * that stays inside the outline. Keep outline corner radii above the bevel.
 */
function extrude(points, length, { bevel = 0.002, holes = [], steps = 1, segments = 0 } = {}) {
  const size = Math.min(bevel, length / 2 - 1e-5);
  const options =
    size > 0
      ? {
          depth: length - 2 * size,
          bevelEnabled: true,
          bevelThickness: size,
          bevelSize: size,
          bevelOffset: -size,
          bevelSegments: segments || (size < 0.0035 ? 1 : size < 0.008 ? 2 : 3),
          steps,
          curveSegments: 1,
        }
      : { depth: length, bevelEnabled: false, steps, curveSegments: 1 };
  const geometry = new ExtrudeGeometry(toShape(points, holes), options);
  if (size > 0) geometry.translate(0, 0, size);
  return crease(geometry);
}

/** Extrude a plan outline of [x, z] points upwards, from y0 to y1. */
function extrudeUp(points, y0, y1, options = {}) {
  const mirror = (list) => list.map((p) => [p[0], -p[1]]);
  const geometry = extrude(mirror(points), y1 - y0, {
    ...options,
    holes: (options.holes || []).map(mirror),
  });
  geometry.rotateX(-Math.PI / 2);
  return move(geometry, 0, y0, 0);
}

/** Extrude a side profile of [z, y] points across X, from x0 to x1. */
function extrudeAcross(points, x0, x1, options = {}) {
  const geometry = extrude(points.map((p) => [-p[0], p[1]]), x1 - x0, options);
  geometry.rotateY(Math.PI / 2);
  return move(geometry, x0, 0, 0);
}

function rbox(width, height, depth, radius = 0.003, segments = 1) {
  return finish(new RoundedBoxGeometry(width, height, depth, segments, radius));
}

/** A plain box for details too small to carry a bevel. */
function box(width, height, depth) {
  return finish(new BoxGeometry(width, height, depth));
}

/** A rounded box whose long (Y) axis runs from `a` to `b`. */
function strut(a, b, width, depth, radius = 0.004, overlap = 0) {
  const direction = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
  const length = Math.hypot(direction[0], direction[1], direction[2]);
  const geometry = move(rbox(width, length + 2 * overlap, depth, radius), 0, length / 2, 0);
  return place(geometry, a, direction);
}

function tube(points, radius, { tubular = 20, radial = 10, closed = false } = {}) {
  const curve = new CatmullRomCurve3(
    points.map((p) => new Vector3(p[0], p[1], p[2])),
    closed,
    'centripetal',
  );
  const sides = radial > 6 ? Math.max(6, Math.round(radial * detail)) : radial;
  return finish(new TubeGeometry(curve, tubular, radius, sides, closed));
}

const hexCache = new Map();

/** A hex bolt head standing on y = 0, optionally on a washer. */
function hexHead(acrossFlats, height, washer = false) {
  const id = `${acrossFlats}:${height}:${washer}`;
  if (!hexCache.has(id)) {
    const radius = acrossFlats / 2 / Math.cos(Math.PI / 6);
    const chamfer = acrossFlats * 0.07;
    const parts = [
      lathe([[radius, 0], [radius, height - chamfer], [radius - chamfer * 1.5, height], [0, height]], 6),
    ];
    if (washer) {
      const rim = radius * 1.3;
      const thick = height * 0.22;
      parts.push(lathe([[rim, 0], [rim, thick], [radius * 0.8, thick]], 10));
    }
    hexCache.set(id, parts.length === 1 ? parts[0] : mergeGeometries(parts, false));
  }
  return hexCache.get(id).clone();
}

function helix(radius, height, turns, wire, perTurn = 7) {
  const points = [];
  const total = Math.round(turns * perTurn);
  for (let i = 0; i <= total; i += 1) {
    const angle = (i / perTurn) * TAU;
    points.push([radius * Math.cos(angle), (height * i) / total, radius * Math.sin(angle)]);
  }
  return tube(points, wire, { tubular: total, radial: 4 });
}

// ----------------------------------------------------------------------------
// Occlusion cast between parts while the engine is assembled
// ----------------------------------------------------------------------------

function bankSpheres(owner, u, v, radius, positions = [-0.18, -0.06, 0.06, 0.18]) {
  const out = [];
  for (const side of ['near', 'far']) {
    for (const w of positions) out.push({ of: `${owner}_${side}`, c: bankPoint(side, u, v, w), r: radius });
  }
  return out;
}

function ringSpheres(owner, y, distance, radius, count) {
  const out = [];
  for (let i = 0; i < count; i += 1) {
    const angle = (i / count) * TAU;
    out.push({ of: owner, c: [distance * Math.cos(angle), y, distance * Math.sin(angle)], r: radius });
  }
  return out;
}

/** Sphere stand-ins for the bulk of each part. A part never shades itself. */
const OCCLUDERS = [
  ...ringSpheres('air', 0.475, 0.12, 0.066, 8),
  { of: 'air', c: [0, 0.48, 0], r: 0.09 },
  { of: 'carb', c: [0, 0.392, 0], r: 0.066 },
  { of: 'carb', c: [0, 0.392, 0.07], r: 0.04 },
  { of: 'carb', c: [0, 0.392, -0.07], r: 0.04 },
  { of: 'intake', c: [0, 0.296, 0], r: 0.07 },
  { of: 'intake', c: [0, 0.262, 0.16], r: 0.05 },
  { of: 'intake', c: [0, 0.262, -0.16], r: 0.05 },
  { of: 'distributor', c: [0, 0.37, DIST_Z], r: 0.05 },
  ...bankSpheres('head', 0.004, V0 + 0.04, 0.062),
  ...bankSpheres('cover', COVER.mid, VC0 + 0.022, 0.05),
  ...bankSpheres('header', 0.165, V0 - 0.02, 0.045, [-0.16, -0.05, 0.06, 0.17]),
  ...bankSpheres('block', 0, DECK - 0.05, 0.08).map((sphere) => ({ ...sphere, of: 'block' })),
  { of: 'block', c: [0, 0.07, 0.15], r: 0.1 },
  { of: 'block', c: [0, 0.07, 0], r: 0.1 },
  { of: 'block', c: [0, 0.07, -0.15], r: 0.1 },
  { of: 'oil_pan', c: [0, -0.1, -0.13], r: 0.085 },
  { of: 'oil_pan', c: [0, -0.06, 0.1], r: 0.065 },
  { of: 'timing_cover', c: [0, 0.07, ZF + 0.02], r: 0.085 },
  { of: 'water_pump', c: [0, PUMP_Y, 0.33], r: 0.055 },
  { of: 'pulleys', c: [0, 0, 0.36], r: 0.075 },
  { of: 'pulleys', c: [0, PUMP_Y, BELT_Z], r: 0.066 },
  { of: 'alternator', c: [ALT[0], ALT[1], 0.31], r: 0.06 },
];

const occluderCache = new Map();

/** The spheres that shade `partId`, flattened to [x, y, z, radius squared]. */
function occludersFor(partId) {
  let flat = occluderCache.get(partId);
  if (!flat) {
    const spheres = OCCLUDERS.filter((sphere) => !partId.startsWith(sphere.of));
    flat = new Float64Array(spheres.length * 4);
    spheres.forEach((sphere, index) => {
      flat.set([sphere.c[0], sphere.c[1], sphere.c[2], sphere.r * sphere.r], index * 4);
    });
    occluderCache.set(partId, flat);
  }
  return flat;
}

/** Bake the occlusion of vertices `from` to `to` (array offsets) into green. */
function castOcclusion(geometry, partId, from = 0, to = Infinity) {
  const position = geometry.attributes.position.array;
  const normal = geometry.attributes.normal.array;
  const colour = geometry.attributes.color.array;
  const spheres = occludersFor(partId);
  const end = Math.min(to, position.length);
  for (let i = from; i < end; i += 3) {
    const px = position[i];
    const py = position[i + 1];
    const pz = position[i + 2];
    const nx = normal[i];
    const ny = normal[i + 1];
    const nz = normal[i + 2];
    let occlusion = 0;
    for (let k = 0; k < spheres.length; k += 4) {
      const dx = spheres[k] - px;
      const dy = spheres[k + 1] - py;
      const dz = spheres[k + 2] - pz;
      const squared = dx * dx + dy * dy + dz * dz;
      const facing = nx * dx + ny * dy + nz * dz;
      if (facing <= 0 || squared < 1e-9) continue;
      const area = spheres[k + 3];
      occlusion += (facing / Math.sqrt(squared)) * (area / Math.max(squared, area));
    }
    colour[i + 1] = 1 - Math.min(0.62, occlusion * 0.85);
  }
}

// ----------------------------------------------------------------------------
// Part builders
// ----------------------------------------------------------------------------

function plugBase(w, offset = 0) {
  return [0.086 + PLUG_AXIS[0] * offset, V0 + 0.0658 + PLUG_AXIS[1] * offset, w];
}

function boltRow(bag, material, head, points, direction, tint = 1) {
  for (const point of points) bag.add(material, place(head.clone(), point, direction), tint);
}

function pistonGeometry() {
  const r = BORE_R - 0.0003;
  const top = DIM.pinToCrown;
  const groove = (y, height) => [
    [r, y],
    [r - 0.0028, y],
    [r - 0.0028, y + height],
    [r, y + height],
  ];
  const body = lathe(
    [
      [0, -0.042],
      [r - 0.0022, -0.042],
      [r - 0.0006, -0.0398],
      [r - 0.0006, 0.009],
      ...groove(0.0098, 0.0046),
      ...groove(0.0188, 0.0022),
      ...groove(0.0266, 0.0022),
      [r, top - 0.0016],
      [r - 0.0016, top],
      [0.033, top],
      [0.029, top - 0.0018],
      [0, top - 0.0018],
    ],
    32,
  );
  // Rings read as dark bands sunk into the lands.
  shade(body, (x, y, z) => (Math.hypot(x, z) < r - 0.0018 && y > 0.009 && y < 0.03 ? 0.3 : 1));
  const parts = [body];
  for (const sign of [1, -1]) {
    parts.push(paint(place(disc(0.0125, 12), [0, 0, sign * (r + 0.0002)], [0, 0, sign]), 0.36));
    parts.push(paint(place(disc(0.0085, 10), [0, 0, sign * (r + 0.0004)], [0, 0, sign]), 0.8));
  }
  return mergeGeometries(parts, false);
}

/** Connecting rod with its small end on the origin and the big end on -Y. */
function rodGeometry() {
  const length = DIM.rodLength;
  const big = circle(0, 0, 0.035, 16, 120 * DEG, 420 * DEG);
  const small = circle(0, length, 0.016, 8, -55 * DEG, 235 * DEG);
  const outline = roundPoly(
    [
      ...big.map((p) => [p[0], p[1], 0]),
      [0.012, 0.047, 0.006],
      [0.0096, length - 0.024, 0.006],
      ...small.map((p) => [p[0], p[1], 0]),
      [-0.0096, length - 0.024, 0.006],
      [-0.012, 0.047, 0.006],
    ],
    0,
  );
  const holes = [circle(0, 0, 0.0268, 14), circle(0, length, 0.0118, 10)];
  const body = move(extrude(outline, 0.021, { bevel: 0.002, holes }), 0, 0, -0.0105);
  const parts = [body];
  for (const sign of [1, -1]) {
    parts.push(paint(move(box(0.0105, 0.068, 0.0006), 0, length * 0.56, sign * 0.0107), 0.5));
    parts.push(place(hexHead(0.011, 0.006), [sign * 0.0295, -0.012, 0], [0, -1, 0]));
  }
  return move(mergeGeometries(parts, false), 0, -length, 0);
}

const BUILDERS = {
  *block({ bag }) {
    const drop = 0.058; // the bores live in separate deck slabs this deep
    const lo = [N * (DECK - drop + 0.092), N * (DECK - drop - 0.092)];
    const li = [N * (DECK - drop - 0.092), N * (DECK - drop + 0.092)];
    const right = [
      [0.124, -0.004, 0.003],
      [0.124, 0.008, 0.012],
      [lo[0] - N * 0.035, lo[1] - N * 0.035, 0.016],
      [lo[0], lo[1], 0],
      [li[0], li[1], 0],
      [0.04, 0.15, 0.012],
    ];
    const profile = roundPoly([...right, ...right.map(([x, y, r]) => [-x, y, r]).reverse()]);
    bag.add('blackSatin', move(extrude(profile, 2 * ZF, { bevel: 0.003, steps: 4 }), 0, 0, -ZF));
    yield;

    for (const side of ['near', 'far']) {
      const local = new Bag();
      const { shift } = BANKS[side];
      const outline = roundRect(-0.0922, -ZF - shift - 0.0002, 0.0922, ZF - shift + 0.0002, 0.003);
      const sides = around(40);
      const bores = W.map((w) => circle(0, w, BORE_R + 0.0002, sides));
      local.add(
        'blackSatin',
        extrudeUp(outline, DECK - drop - 0.008, DECK, { bevel: 0.0015, holes: bores }),
      );
      yield;
      for (const w of W) {
        // Inward-facing liner wall, darkening with depth, and a floor far down the bore.
        const liner = lathe([[BORE_R - 0.0003, 0], [BORE_R - 0.0003, -(drop - 0.0012)]], 40);
        shade(liner, (x, y) => mix(0.2, 0.85, smooth(-0.05, 0, y)));
        local.add('steel', move(liner, 0, DECK - 0.0008, w));
        yield;
        local.add('steel', move(ring(BORE_R - 0.0003, BORE_R + 0.0042, 40), 0, DECK + 0.0002, w), 0.9);
        yield;
        local.add('blackSatin', move(disc(BORE_R, 20), 0, DECK - drop + 0.0008, w), 0.25);
        yield;
      }
      for (const w of [-PITCH, 0, PITCH]) {
        const plug = lathe([[0.0205, 0], [0.0205, 0.0024], [0.0185, 0.0024], [0.0165, 0.0005], [0, 0.0005]], 16);
        local.add('steel', place(plug, [0.0922, DECK - 0.072, w], [1, 0, 0]));
        yield;
      }
      local.apply(bankMatrix(side));
      bag.take(local);
      yield;
    }

    for (const z of [ZF - 0.012, -ZF + 0.012]) {
      bag.add('blackSatin', move(rbox(0.19, 0.078, 0.022, 0.003), 0, 0.1865, z));
      yield;
    }
    for (let k = -2; k <= 2; k += 1) {
      bag.add('blackSatin', move(rbox(0.105, 0.046, 0.026, 0.004), 0, -0.027, k * PITCH));
      yield;
    }
    bag.add('blackSatin', move(rbox(0.086, 0.024, 0.086, 0.005), 0.15, 0.004, -0.175));
    yield;
    const camPlug = lathe([[0.025, 0], [0.025, 0.0026], [0.0225, 0.0026], [0.02, 0.0006], [0, 0.0006]], 20);
    bag.add('steel', place(camPlug, [0, CAM_Y, ZF], [0, 0, 1]));
    yield;
    for (const x of [-0.128, 0.128]) {
      bag.add('blackSatin', place(disc(0.012, 12), [x, 0.146, ZF + 0.0004], [0, 0, 1]), 0.2);
      yield;
    }
  },

  *head(out) {
    out.local = true;
    const { bag } = out;
    const A = 'machinedAluminium';
    const half = 0.2435;
    const profile = roundPoly(
      [[-0.088, V0], [0.098, V0], [0.098, V0 + 0.05], [0.074, HEAD_TOP], [-0.088, HEAD_TOP]],
      0.003,
    );
    bag.add(A, move(extrude(profile, 2 * half, { bevel: 0.003, steps: 3 }), 0, 0, -half));
    yield;
    const outer = roundRect(COVER.u0 - 0.004, -COVER.half - 0.004, COVER.u1 + 0.004, COVER.half + 0.004, 0.022);
    const inner = roundRect(COVER.u0 + 0.008, -COVER.half + 0.008, COVER.u1 - 0.008, COVER.half - 0.008, 0.012);
    bag.add(A, extrudeUp(outer, HEAD_TOP - 0.002, RAIL_TOP, { bevel: 0.0015, holes: [inner] }));
    yield;
    for (const w of W) {
      bag.add(A, move(rbox(0.01, 0.04, 0.06, 0.003), 0.101, V0 + 0.027, w));
      yield;
      bag.add(A, move(box(0.0012, 0.026, 0.036), 0.1062, V0 + 0.027, w), 0.08);
      yield;
      const boss = lathe([[0.015, 0], [0.015, 0.0025], [0.014, 0.0035], [0.0075, 0.0035]], 14);
      bag.add(A, place(boss, plugBase(w, -0.0005), PLUG_AXIS));
      yield;
      bag.add(A, move(box(0.0012, 0.042, 0.03), -0.0884, V0 + 0.045, w), 0.08);
      yield;
    }
    for (const w of [-0.13, 0.13]) {
      for (const u of [COVER.u0 - 0.008, COVER.u1 + 0.008]) {
        bag.add(A, move(rbox(0.016, 0.0125, 0.024, 0.002), u, HEAD_TOP + 0.0048, w));
        yield;
      }
    }
    const bolts = [[COVER.mid, HEAD_TOP, -0.212], [COVER.mid, HEAD_TOP, 0.212]];
    for (const w of [-PITCH, 0, PITCH]) bolts.push([-0.046, HEAD_TOP, w], [0.04, HEAD_TOP, w]);
    boltRow(bag, A, hexHead(0.016, 0.008, true), bolts, [0, 1, 0], 0.62);
    yield;
    for (const u of [-0.05, 0.05]) {
      bag.add(A, place(disc(0.006, 10), [u, V0 + 0.04, half + 0.0003], [0, 0, 1]), 0.1);
      yield;
    }
  },

  *cover(out) {
    out.local = true;
    const { bag } = out;
    const R = 'redGloss';
    const M = 'machinedAluminium';
    const outline = roundRect(COVER.u0, -COVER.half, COVER.u1, COVER.half, 0.024, 4);
    bag.add(R, extrudeUp(outline, VC0 - 0.006, VC_TOP, { bevel: 0.011, segments: 4 }));
    yield;
    const lip = roundRect(COVER.u0 - 0.004, -COVER.half - 0.004, COVER.u1 + 0.004, COVER.half + 0.004, 0.027, 4);
    bag.add(R, extrudeUp(lip, VC0, VC0 + 0.0045, { bevel: 0.0012 }));
    yield;
    // Nine fins, in their own shade, with the tops machined back to bare metal.
    bag.add(R, move(box(0.088, 0.0006, 0.292), COVER.mid, VC_TOP + 0.0002, -0.035), 0.6);
    yield;
    for (let i = 0; i < 9; i += 1) {
      const u = COVER.mid - 0.0384 + i * 0.0096;
      const fin = move(rbox(0.0044, 0.0075, 0.29, 0.0013), u, VC_TOP + 0.003, -0.035);
      shade(fin, (x, y) => mix(0.55, 1, smooth(VC_TOP, VC_TOP + 0.005, y)));
      bag.add(R, fin);
      yield;
      bag.add(M, move(box(0.0028, 0.0006, 0.285), u, VC_TOP + 0.0069, -0.035));
      yield;
    }
    const bolt = hexHead(0.011, 0.005, true);
    for (const w of [-0.13, 0.13]) {
      for (const u of [COVER.u0 - 0.008, COVER.u1 + 0.008]) {
        bag.add(R, move(rbox(0.016, 0.0045, 0.024, 0.002), u, VC0 + 0.00225, w));
        yield;
        bag.add(M, place(bolt.clone(), [u, VC0 + 0.0045, w], [0, 1, 0]), 0.7);
        yield;
      }
    }
    // Push-in breather near the front.
    bag.add(M, move(lathe([[0.011, 0], [0.011, 0.012]], 14), COVER.mid, VC_TOP, 0.166));
    yield;
    const cap = lathe(
      roundPoly([[0.012, 0.01], [0.029, 0.01, 0.002], [0.031, 0.021, 0.004], [0.023, 0.031, 0.008], [0, 0.034]], 0, false),
      24,
    );
    bag.add(M, move(cap, COVER.mid, VC_TOP, 0.166));
    yield;
  },

  *gasket(out) {
    out.local = true;
    const outline = roundRect(-0.089, -0.2438, 0.093, 0.2438, 0.004);
    const sides = around(40);
    const holes = W.map((w) => circle(0, w, 0.0532, sides));
    out.bag.add('copper', extrudeUp(outline, DECK + 0.0003, DECK + 0.0017, { bevel: 0, holes }), 0.8);
    yield;
    for (const w of W) {
      out.bag.add('copper', move(ring(0.0532, 0.0566, 40), 0, DECK + 0.00185, w));
      yield;
    }
  },

  *intake({ bag }) {
    const A = 'castAluminium';
    bag.add(A, move(rbox(0.2, 0.006, 0.47, 0.002), 0, 0.231, 0));
    yield;
    bag.add(A, move(rbox(0.118, 0.062, 0.128, 0.014, 2), 0, 0.304, 0));
    yield;
    bag.add(A, move(rbox(0.15, 0.014, 0.15, 0.004), 0, 0.3392, 0));
    yield;
    const bolt = hexHead(0.013, 0.007);
    for (const side of ['near', 'far']) {
      const sign = BANKS[side].sign;
      const flange = move(rbox(0.012, 0.066, 0.44, 0.003), -0.088 - GAP - 0.006, V0 + 0.045, 0);
      bag.add(A, transform(flange, bankMatrix(side)));
      yield;
      for (const w of [-0.205, -PITCH, 0, PITCH, 0.205]) {
        const at = bankPoint(side, -0.088 - GAP - 0.012, V0 + 0.05, w);
        bag.add(A, place(bolt.clone(), at, [-sign * N, N, 0]), 0.6);
        yield;
      }
      for (const w of W) {
        const z = w + BANKS[side].shift;
        const path = [
          [sign * 0.03, 0.302, z * 0.22],
          [sign * 0.072, 0.314, z * 0.6],
          bankPoint(side, -0.136, V0 + 0.045, w),
          bankPoint(side, -0.096, V0 + 0.045, w),
        ];
        bag.add(A, tube(path, 0.021, { tubular: 12, radial: 12 }));
        yield;
      }
    }
    // Water crossover and thermostat neck at the front.
    bag.add(A, move(rbox(0.27, 0.034, 0.036, 0.011, 2), 0, 0.266, 0.2205));
    yield;
    const housing = lathe(
      roundPoly([[0.034, 0], [0.034, 0.007, 0.002], [0.027, 0.007, 0.002], [0.022, 0.02, 0.005], [0, 0.02]], 0, false),
      20,
    );
    bag.add(A, move(housing, 0, 0.283, 0.2205));
    yield;
    const neck = [[0, 0.297, 0.2205], [0, 0.318, 0.23], [0, 0.334, 0.258], [0, 0.34, 0.288]];
    bag.add(A, tube(neck, 0.017, { tubular: 10, radial: 12 }));
    yield;
    const outward = [0, 0.19, 0.98];
    bag.add(A, place(lathe([[0.0168, -0.006], [0.0196, -0.004], [0.0196, 0], [0.013, 0]], 14), neck[3], outward));
    yield;
    bag.add(A, place(disc(0.013, 12), [0, 0.3401, 0.2883], outward), 0.06);
    yield;
    boltRow(bag, A, hexHead(0.011, 0.006), [[-0.028, 0.29, 0.2205], [0.028, 0.29, 0.2205]], [0, 1, 0], 0.6);
    yield;
    // Rear rail and the distributor boss.
    bag.add(A, move(rbox(0.19, 0.022, 0.03, 0.006), 0, 0.245, -0.22));
    yield;
    bag.add(A, move(lathe([[0.03, 0], [0.03, 0.027], [0.027, 0.03], [0.02, 0.03]], 16), 0, 0.234, DIST_Z));
    yield;
  },

  *carb({ bag }) {
    const G = 'zincGold';
    const y0 = 0.3475;
    bag.add(G, move(rbox(0.142, 0.02, 0.142, 0.004), 0, y0 + 0.01, 0));
    yield;
    bag.add(G, move(rbox(0.11, 0.052, 0.1, 0.006, 2), 0, y0 + 0.046, 0));
    yield;
    const screw = hexHead(0.007, 0.003);
    for (const sign of [1, -1]) {
      bag.add(G, move(rbox(0.104, 0.05, 0.013, 0.002), 0, y0 + 0.045, sign * 0.0565), 0.78);
      yield;
      bag.add(G, move(rbox(0.096, 0.046, 0.044, 0.009, 2), 0, y0 + 0.045, sign * 0.085));
      yield;
      for (const x of [-0.036, 0.036]) {
        for (const dy of [-0.015, 0.015]) {
          bag.add('steel', place(screw.clone(), [x, y0 + 0.045 + dy, sign * 0.107], [0, 0, sign]));
          yield;
        }
      }
      bag.add('steel', place(hexHead(0.016, 0.014), [0.048, y0 + 0.05, sign * 0.085], [1, 0, 0]));
      yield;
    }
    // Dual-feed fuel log along the near side.
    const log = [
      [0.06, y0 + 0.05, 0.085],
      [0.076, y0 + 0.05, 0.083],
      [0.084, y0 + 0.05, 0.062],
      [0.084, y0 + 0.05, -0.062],
      [0.076, y0 + 0.05, -0.083],
      [0.06, y0 + 0.05, -0.085],
    ];
    bag.add('steel', tube(log, 0.0042, { tubular: 22, radial: 6 }));
    yield;
    bag.add('steel', place(hexHead(0.014, 0.016), [0.084, y0 + 0.042, 0], [0, 1, 0]));
    yield;
    // Air horn with four dark venturis.
    const top = y0 + 0.072;
    bag.add(G, move(lathe([[0.0655, 0], [0.0655, 0.019], [0.064, 0.0205], [0.058, 0.0205], [0.058, 0.002]], 32), 0, top, 0));
    yield;
    bag.add(G, move(disc(0.058, 24), 0, top + 0.0022, 0), 0.1);
    yield;
    for (const x of [-0.024, 0.024]) {
      for (const z of [-0.027, 0.027]) {
        bag.add(G, move(lathe([[0.02, 0], [0.02, 0.007], [0.0165, 0.007], [0.0165, 0.001]], 12), x, top + 0.0024, z));
        yield;
        bag.add(G, move(disc(0.0165, 12), x, top + 0.0036, z), 0.03);
        yield;
      }
    }
    // Electric choke cap on the near side, vacuum pod on the far side.
    const choke = lathe([[0.027, 0], [0.027, 0.02], [0.023, 0.024], [0, 0.024]], 20);
    bag.add(G, place(choke, [0.055, y0 + 0.056, 0.02], [1, 0, 0]), 0.07);
    yield;
    const pod = lathe([[0.028, 0], [0.028, 0.016], [0.02, 0.022], [0, 0.022]], 20);
    bag.add(G, place(pod, [-0.055, y0 + 0.05, -0.03], [-1, 0, 0]));
    yield;
    // Throttle arm and the four mounting nuts.
    bag.add('steel', strut([0.0735, y0 + 0.004, -0.03], [0.0735, y0 + 0.05, -0.048], 0.005, 0.014, 0.002));
    yield;
    const corners = [-0.058, 0.058].flatMap((x) => [-0.058, 0.058].map((z) => [x, y0 + 0.02, z]));
    boltRow(bag, 'steel', hexHead(0.012, 0.007, true), corners, [0, 1, 0]);
    yield;
  },

  *air_base({ bag }) {
    const pan = lathe(
      [
        [0.067, 0.427],
        [0.15, 0.427],
        [0.1575, 0.4318],
        [0.179, 0.432],
        [0.181, 0.4385],
        [0.1795, 0.44],
        [0.176, 0.4378],
        [0.176, 0.434],
        [0.158, 0.434],
        [0.15, 0.429],
        [0.073, 0.429],
        [0.073, 0.4405],
        [0.067, 0.4405],
        [0.067, 0.427],
      ],
      48,
    );
    bag.add('blackSatin', pan);
    yield;
  },

  *air_filter({ bag }) {
    const pleats = Math.round(64 * (detail < 1 ? 0.75 : 1));
    const star = [];
    for (let i = 0; i < pleats * 2; i += 1) {
      const angle = (i / (pleats * 2)) * TAU;
      const radius = i % 2 ? 0.1604 : 0.1716;
      star.push([radius * Math.cos(angle), radius * Math.sin(angle)]);
    }
    const media = extrudeUp(star, 0.4375, 0.4895, { bevel: 0 });
    // Deep in each pleat the paper is in its own shadow.
    shade(media, (x, y, z) => mix(0.3, 1, smooth(0.1604, 0.1716, Math.hypot(x, z))));
    bag.add('yellowPaper', media);
    yield;
    for (const y of [0.4345, 0.489]) {
      const band = lathe([[0.146, 0], [0.173, 0], [0.1736, 0.0006], [0.1736, 0.0029], [0.173, 0.0035], [0.146, 0.0035]], 48);
      bag.add('rubber', move(band, 0, y, 0));
      yield;
    }
  },

  *air_lid({ bag }) {
    // A domed, stepped lid: the curvature is what lets chrome show a gradient.
    const lid = lathe(
      roundPoly(
        [
          [0, 0.4931],
          [0.171, 0.4931, 0.002],
          [0.1795, 0.4895, 0.0012],
          [0.1826, 0.4925, 0.0016],
          [0.1812, 0.4962, 0.0016],
          [0.1755, 0.4978, 0.002],
          [0.16, 0.4978, 0.0012],
          [0.1575, 0.5005, 0.0012],
          [0.14, 0.5038],
          [0.11, 0.5074],
          [0.075, 0.5099],
          [0.045, 0.511],
          [0.034, 0.511, 0.002],
          [0.026, 0.5075, 0.002],
          [0, 0.5075],
        ],
        0,
        false,
      ),
      56,
    );
    bag.add('chrome', lid);
    yield;
    bag.add('steel', move(lathe([[0.003, 0], [0.003, 0.014], [0, 0.014]], 8), 0, 0.5075, 0));
    yield;
    bag.add('steel', move(lathe([[0.0075, 0], [0.0075, 0.005], [0.0055, 0.008], [0.003, 0.008]], 12), 0, 0.5075, 0));
    yield;
    for (const sign of [1, -1]) {
      const wing = rbox(0.017, 0.0105, 0.0032, 0.0013);
      wing.rotateZ(sign * 0.42);
      bag.add('steel', move(wing, sign * 0.0125, 0.5158, 0));
      yield;
    }
  },

  *distributor({ bag }) {
    const z = DIST_Z;
    const body = lathe(
      roundPoly([[0.016, 0.262], [0.016, 0.3, 0.003], [0.03, 0.312, 0.003], [0.03, 0.345, 0.002], [0.047, 0.352, 0.002], [0.047, 0.3665]], 0, false),
      24,
    );
    bag.add('castAluminium', move(body, 0, 0, z));
    yield;
    const cap = lathe(
      roundPoly(
        [[0.049, 0.3675], [0.052, 0.372, 0.002], [0.052, 0.394, 0.004], [0.045, 0.403, 0.004], [0.031, 0.407, 0.003], [0.025, 0.419, 0.003], [0, 0.42]],
        0,
        false,
      ),
      32,
    );
    bag.add('redGloss', move(cap, 0, 0, z));
    yield;
    for (let i = 0; i < 8; i += 1) {
      const angle = (i + 0.5) * (TAU / 8);
      const dir = [Math.cos(angle) * 0.5, 0.866, Math.sin(angle) * 0.5];
      const base = [Math.cos(angle) * 0.04, 0.3975, z + Math.sin(angle) * 0.04];
      const tower = lathe([[0.0085, 0], [0.0085, 0.013], [0.0065, 0.016], [0, 0.016]], 8);
      bag.add('redGloss', place(tower, base, dir));
      yield;
      const tip = base.map((value, c) => value + dir[c] * 0.0163);
      bag.add('redGloss', place(disc(0.0042, 8), tip, dir), 0.12);
      yield;
    }
    // Vacuum advance can and hold-down bolt.
    const can = lathe([[0.027, 0], [0.027, 0.012], [0.019, 0.018], [0, 0.018]], 16);
    bag.add('castAluminium', place(can, [0.04, 0.33, z + 0.03], [0.8, 0, 0.6]));
    yield;
    bag.add('castAluminium', strut([0.012, 0.33, z + 0.01], [0.04, 0.33, z + 0.03], 0.012, 0.012, 0.003));
    yield;
    bag.add('castAluminium', place(hexHead(0.013, 0.007, true), [0.03, 0.264, z + 0.022], [0, 1, 0]), 0.6);
    yield;
  },

  *plug_wires({ bag }) {
    // Right-angle boots: a sleeve over the plug's terminal and an elbow pointing
    // up, so each lead can drop straight down from the edge of the valve cover.
    const sleeve = lathe([[0.0078, 0], [0.0078, 0.027], [0.0066, 0.03], [0, 0.03]], 8);
    const elbow = lathe([[0.0074, -0.006], [0.0074, 0.02], [0.0056, 0.026], [0, 0.026]], 8);
    const cuff = lathe([[0.0098, 0], [0.0098, 0.02], [0.006, 0.027], [0, 0.027]], 8);
    for (const side of ['near', 'far']) {
      const sign = BANKS[side].sign;
      const axis = [sign * N * (PLUG_AXIS[1] + PLUG_AXIS[0]), N * (PLUG_AXIS[1] - PLUG_AXIS[0]), 0];
      for (const [index, w] of W.entries()) {
        // Front cylinders take the inner lanes so nothing has to cross.
        const lane = COVER.mid + 0.028 + index * 0.0088;
        const angle = (sign > 0 ? [67.5, 22.5, -22.5, -67.5] : [112.5, 157.5, 202.5, 247.5])[index] * DEG;
        const dir = [Math.cos(angle) * 0.5, 0.866, Math.sin(angle) * 0.5];
        const tower = [Math.cos(angle) * 0.04, 0.3975, DIST_Z + Math.sin(angle) * 0.04];
        const start = tower.map((value, c) => value + dir[c] * 0.02);
        const terminal = bankPoint(side, ...plugBase(w, 0.05));
        const path = [
          start,
          start.map((value, c) => value + dir[c] * 0.022),
          bankPoint(side, lane - 0.01, VC_TOP + 0.02, -0.25),
          bankPoint(side, lane, VC_TOP + 0.0125, -0.214),
          bankPoint(side, lane, VC_TOP + 0.0125, w - 0.036),
          bankPoint(side, COVER.u1 + 0.004, VC_TOP + 0.006, w - 0.008),
          [terminal[0] + sign * 0.001, terminal[1] + 0.05, terminal[2]],
          [terminal[0], terminal[1] + 0.02, terminal[2]],
        ];
        bag.add('yellow', tube(path, 0.0038, { tubular: 36, radial: 5 }));
        yield;
        bag.add('rubber', place(sleeve.clone(), bankPoint(side, ...plugBase(w, 0.028)), axis));
        yield;
        bag.add('rubber', place(elbow.clone(), terminal, [0, 1, 0]));
        yield;
        bag.add('rubber', place(cuff.clone(), tower.map((value, c) => value + dir[c] * 0.004), dir));
        yield;
      }
      for (const w of [-0.14, -0.02]) {
        const loom = move(rbox(0.046, 0.007, 0.012, 0.002), COVER.mid + 0.041, VC_TOP + 0.0125, w);
        bag.add('rubber', transform(loom, bankMatrix(side)));
        yield;
      }
    }
  },

  *timing_cover({ bag }) {
    const A = 'castAluminium';
    const z0 = ZF + GAP;
    const shell = tangentLoop([{ x: 0, y: 0, r: 0.068 }, { x: 0, y: CAM_Y, r: 0.097 }]).outline(10 * DEG);
    bag.add(A, move(extrude(shell, 0.04, { bevel: 0.007, segments: 3 }), 0, 0, z0));
    yield;
    const flange = tangentLoop([{ x: 0, y: 0, r: 0.076 }, { x: 0, y: CAM_Y, r: 0.105 }]);
    bag.add(A, move(extrude(flange.outline(10 * DEG), 0.0055, { bevel: 0.0012 }), 0, 0, z0));
    yield;
    const bolt = hexHead(0.01, 0.005);
    const rim = tangentLoop([{ x: 0, y: 0, r: 0.0722 }, { x: 0, y: CAM_Y, r: 0.1012 }]);
    for (let i = 0; i < 10; i += 1) {
      const [x, y] = rim.at(((i + 0.5) / 10) * rim.length);
      bag.add(A, place(bolt.clone(), [x, y, z0 + 0.0055], [0, 0, 1]), 0.62);
      yield;
    }
    const front = z0 + 0.04;
    const seal = lathe([[0.045, 0], [0.045, 0.008], [0.041, 0.012], [0.031, 0.012]], 28);
    bag.add(A, place(seal, [0, 0, front - 0.001], [0, 0, 1]));
    yield;
    bag.add(A, place(ring(0.024, 0.031, 24), [0, 0, front + 0.0105], [0, 0, 1]), 0.08);
    yield;
    for (const angle of [-26, 0, 26]) {
      const a = (90 + angle) * DEG;
      const from = [Math.cos(a) * 0.05, Math.sin(a) * 0.05, front - 0.002];
      const to = [Math.cos(a) * 0.13 + Math.sin(angle * DEG) * 0.012, CAM_Y + 0.04, front - 0.002];
      bag.add(A, strut(from, to, 0.006, 0.006, 0.0025));
      yield;
    }
  },

  *water_pump({ bag }) {
    const A = 'castAluminium';
    const y = PUMP_Y;
    const body = lathe(
      roundPoly(
        [[0, 0.3], [0.06, 0.3, 0.002], [0.06, 0.31, 0.003], [0.052, 0.318, 0.006], [0.036, 0.345, 0.006], [0.024, 0.352, 0.003], [0.022, 0.372, 0.002], [0.012, 0.374], [0.012, 0.38]],
        0,
        false,
      ),
      32,
    );
    bag.add(A, move(alongZ(body), 0, y, 0));
    yield;
    const hub = lathe([[0.012, 0.3795], [0.029, 0.3795], [0.03, 0.3805], [0.03, 0.385], [0.029, 0.386], [0.0095, 0.386]], 20);
    bag.add('steel', move(alongZ(hub), 0, y, 0));
    yield;
    const bolt = hexHead(0.012, 0.007);
    for (const sign of [1, -1]) {
      // Slim cast legs reaching back to the block either side of the timing cover.
      bag.add(A, strut([sign * 0.042, y - 0.006, 0.322], [sign * 0.126, 0.15, 0.274], 0.026, 0.03, 0.011, 0.008));
      yield;
      bag.add(A, move(rbox(0.04, 0.046, 0.013, 0.006), sign * 0.128, 0.146, ZF + GAP + 0.0065));
      yield;
      for (const dy of [-0.013, 0.013]) {
        bag.add(A, place(bolt.clone(), [sign * 0.128, 0.146 + dy, ZF + GAP + 0.013], [0, 0, 1]), 0.6);
        yield;
      }
    }
    // Lower hose neck, on the far side.
    const neck = [[-0.025, y - 0.02, 0.325], [-0.06, y - 0.04, 0.334], [-0.092, y - 0.054, 0.34], [-0.112, y - 0.062, 0.342]];
    bag.add(A, tube(neck, 0.019, { tubular: 8, radial: 12 }));
    yield;
    const outward = [-0.92, -0.37, 0.09];
    bag.add(A, place(lathe([[0.0188, -0.007], [0.0215, -0.005], [0.0215, 0], [0.015, 0]], 14), neck[3], outward));
    yield;
    bag.add(A, place(disc(0.015, 12), [neck[3][0] - 0.0003, neck[3][1] - 0.0001, neck[3][2]], outward), 0.06);
    yield;
  },

  *pulleys(out) {
    const S = 'steel';
    const crank = { center: [0, 0, 0], ratio: 1, bag: new Bag() };
    const balancer = lathe(
      roundPoly([[0.02, 0.308], [0.081, 0.308], [0.084, 0.311], [0.084, 0.335], [0.081, 0.338], [0.02, 0.338]], 0, false),
      44,
    );
    shade(balancer, (x, y, z) => {
      const radius = Math.hypot(x, z);
      return radius > 0.062 && radius < 0.066 ? 0.04 : 0.13;
    });
    crank.bag.add(S, alongZ(balancer));
    yield;
    const mark = [Math.cos(1.9), Math.sin(1.9), 0];
    crank.bag.add(S, place(box(0.0035, 0.004, 0.02), [mark[0] * 0.0835, mark[1] * 0.0835, 0.322], mark));
    yield;
    const cup = lathe(
      roundPoly(
        [
          [0.022, 0.3385],
          [0.05, 0.3385, 0.003],
          [0.07, 0.372, 0.002],
          [0.083, 0.372, 0.001],
          [0.083, 0.3765],
          [0.072, 0.3835],
          [0.072, 0.3865],
          [0.083, 0.3935],
          [0.083, 0.398, 0.001],
          [0.068, 0.398, 0.001],
          [0.066, 0.376, 0.004],
          [0.047, 0.343, 0.004],
          [0.022, 0.343],
        ],
        0,
        false,
      ),
      44,
    );
    crank.bag.add(S, alongZ(cup));
    yield;
    const small = hexHead(0.012, 0.006);
    for (let i = 0; i < 3; i += 1) {
      const angle = (i / 3) * TAU + 0.5;
      crank.bag.add(S, place(small.clone(), [Math.cos(angle) * 0.036, Math.sin(angle) * 0.036, 0.343], [0, 0, 1]), 0.8);
      yield;
    }
    crank.bag.add(S, place(hexHead(0.024, 0.012, true), [0, 0, 0.343], [0, 0, 1]), 0.8);
    yield;

    const pump = { center: [0, PUMP_Y, 0], ratio: PULLEY.crank / PULLEY.pump, bag: new Bag() };
    const wheel = lathe(
      roundPoly(
        [
          [0.01, 0.3865],
          [0.046, 0.3865, 0.003],
          [0.062, 0.372, 0.002],
          [0.078, 0.372, 0.001],
          [0.078, 0.3765],
          [0.067, 0.3835],
          [0.067, 0.3865],
          [0.078, 0.3935],
          [0.078, 0.398, 0.001],
          [0.064, 0.398, 0.002],
          [0.048, 0.3905, 0.003],
          [0.01, 0.3905],
        ],
        0,
        false,
      ),
      44,
    );
    pump.bag.add(S, move(alongZ(wheel), 0, PUMP_Y, 0));
    yield;
    const stud = hexHead(0.011, 0.006);
    for (let i = 0; i < 4; i += 1) {
      const angle = (i / 4) * TAU + 0.3;
      const hole = angle + TAU / 8;
      pump.bag.add(S, place(stud.clone(), [Math.cos(angle) * 0.022, PUMP_Y + Math.sin(angle) * 0.022, 0.3905], [0, 0, 1]), 0.8);
      yield;
      pump.bag.add(S, place(disc(0.0085, 12), [Math.cos(hole) * 0.036, PUMP_Y + Math.sin(hole) * 0.036, 0.3907], [0, 0, 1]), 0.04);
      yield;
    }
    pump.bag.add(S, place(disc(0.0075, 10), [0, PUMP_Y, 0.3907], [0, 0, 1]), 0.1);
    yield;
    out.spinners.push(crank, pump);
  },

  *alternator(out) {
    const { bag } = out;
    const A = 'castAluminium';
    const [ax, ay] = ALT;
    const body = lathe(
      roundPoly(
        [
          [0, 0.268],
          [0.05, 0.268, 0.006],
          [0.064, 0.276, 0.004],
          [0.066, 0.296],
          [0.0685, 0.297],
          [0.0685, 0.322],
          [0.066, 0.323],
          [0.066, 0.342, 0.005],
          [0.058, 0.352, 0.004],
          [0.022, 0.352],
          [0.022, 0.36],
          [0.01, 0.36],
          [0.01, 0.366],
        ],
        0,
        false,
      ),
      44,
    );
    // The stator stack between the two housings is bare, darker steel.
    shade(body, (x, y, z) => (y > 0.2972 && y < 0.3218 && Math.hypot(x, z) > 0.067 ? 0.32 : 1));
    bag.add(A, move(alongZ(body), ax, ay, 0));
    yield;
    for (const z of [0.281, 0.3285]) {
      const ribs = extrude(gearOutline(24, 0.0655, 0.0692, 0.2), 0.012, { bevel: 0 });
      bag.add(A, move(ribs, ax, ay, z));
      yield;
    }
    for (let i = 0; i < 8; i += 1) {
      const slot = move(box(0.02, 0.0075, 0.0008), 0.042, 0, 0);
      slot.rotateZ((i / 8) * TAU);
      bag.add(A, move(slot, ax, ay, 0.3522), 0.06);
      yield;
    }
    bag.add(A, move(rbox(0.032, 0.036, 0.05, 0.007), ax + 0.028, ay - 0.074, 0.31));
    yield;
    bag.add(A, move(rbox(0.024, 0.032, 0.014, 0.005), ax - 0.004, ay + 0.078, 0.345));
    yield;
    const bolt = hexHead(0.014, 0.008, true);
    bag.add(A, place(bolt.clone(), [ax + 0.028, ay - 0.078, 0.335], [0, 0, 1]), 0.6);
    yield;
    bag.add(A, place(bolt.clone(), [ax - 0.004, ay + 0.081, 0.352], [0, 0, 1]), 0.6);
    yield;

    const rotor = { center: [ax, ay, 0], ratio: PULLEY.crank / PULLEY.alt, bag: new Bag() };
    const fan = lathe([[0.012, 0.3625], [0.06, 0.3625], [0.06, 0.3645], [0.012, 0.3645]], 24);
    rotor.bag.add('steel', move(alongZ(fan), ax, ay, 0));
    yield;
    for (let i = 0; i < 12; i += 1) {
      const blade = move(box(0.017, 0.0022, 0.009), 0.05, 0, 0);
      blade.rotateZ(0.5);
      blade.rotateZ((i / 12) * TAU);
      rotor.bag.add('steel', move(blade, ax, ay, 0.3595), 0.8);
      yield;
    }
    const wheel = lathe(
      [
        [0.008, 0.366],
        [0.024, 0.366],
        [0.034, 0.372],
        [0.034, 0.3765],
        [0.026, 0.3835],
        [0.026, 0.3865],
        [0.034, 0.3935],
        [0.034, 0.398],
        [0.016, 0.398],
        [0.016, 0.404],
        [0, 0.404],
      ],
      28,
    );
    rotor.bag.add('steel', move(alongZ(wheel), ax, ay, 0));
    yield;
    rotor.bag.add('steel', place(hexHead(0.016, 0.008), [ax, ay, 0.404], [0, 0, 1]), 0.8);
    yield;
    out.spinners.push(rotor);
  },

  *belt(out) {
    const circles = (offset) => [
      { x: 0, y: 0, r: PULLEY.crank + offset },
      { x: 0, y: PUMP_Y, r: PULLEY.pump + offset },
      { x: ALT[0], y: ALT[1], r: PULLEY.alt + offset },
    ];
    const outer = tangentLoop(circles(0.0045)).outline();
    const inner = tangentLoop(circles(-0.0045)).outline();
    const band = extrude(outer, 0.011, { bevel: 0.0015, holes: [inner] });
    out.bag.add('rubber', move(band, 0, 0, BELT_Z - 0.0055));
    yield;

    // Printed marks ride round with the belt, so a turning crank can be seen.
    const loop = tangentLoop(circles(0.0047));
    const count = 9;
    const point = [0, 0, 0];
    out.instanced.push({
      material: 'yellow',
      geometry: box(0.016, 0.0006, 0.0052),
      count,
      update(mesh, state) {
        const travel = state.crank * PULLEY.crank;
        for (let i = 0; i < count; i += 1) {
          loop.at((i / count) * loop.length + travel, point);
          _matrix.makeRotationZ(point[2]).setPosition(point[0], point[1], BELT_Z);
          mesh.setMatrixAt(i, _matrix);
        }
        mesh.instanceMatrix.needsUpdate = true;
      },
    });
  },

  *timing_set(out) {
    const S = 'steel';
    const crank = { center: [0, 0, 0], ratio: 1, bag: new Bag() };
    const small = extrude(gearOutline(22, SPROCKET.crank - 0.0034, SPROCKET.crank + 0.0022), 0.012, {
      bevel: 0,
      holes: [circle(0, 0, 0.0185, 14)],
    });
    crank.bag.add(S, move(small, 0, 0, CHAIN_Z - 0.006));
    yield;
    const hub = lathe([[0.0185, 0], [0.026, 0], [0.026, 0.016], [0.0185, 0.016]], 16);
    crank.bag.add(S, move(alongZ(hub), 0, 0, CHAIN_Z - 0.009), 0.8);
    yield;
    crank.bag.add(S, move(box(0.006, 0.004, 0.004), 0, 0.0195, CHAIN_Z + 0.0062), 0.2);
    yield;

    const cam = { center: [0, CAM_Y, 0], ratio: 0.5, bag: new Bag() };
    const holes = [0, 1, 2].map((i) => {
      const angle = (i / 3) * TAU + 0.5;
      return circle(Math.cos(angle) * 0.04, Math.sin(angle) * 0.04, 0.0165, 14);
    });
    const big = extrude(gearOutline(44, SPROCKET.cam - 0.0034, SPROCKET.cam + 0.0022), 0.01, { bevel: 0, holes });
    cam.bag.add(S, move(big, 0, CAM_Y, CHAIN_Z - 0.005));
    yield;
    const bolt = hexHead(0.011, 0.005);
    for (let i = 0; i < 3; i += 1) {
      const angle = (i / 3) * TAU + 0.5 + TAU / 6;
      cam.bag.add(S, place(bolt.clone(), [Math.cos(angle) * 0.019, CAM_Y + Math.sin(angle) * 0.019, CHAIN_Z + 0.005], [0, 0, 1]), 0.75);
      yield;
    }
    out.spinners.push(crank, cam);

    const loop = tangentLoop([{ x: 0, y: 0, r: SPROCKET.crank }, { x: 0, y: CAM_Y, r: SPROCKET.cam }]);
    const count = Math.round(loop.length / 0.009525);
    const pitch = loop.length / count;
    const stadium = [
      ...circle(pitch * 0.42, 0, 0.0041, 3, -Math.PI / 2, Math.PI / 2),
      ...circle(-pitch * 0.42, 0, 0.0041, 3, Math.PI / 2, 1.5 * Math.PI),
    ];
    const link = move(extrude(stadium, 0.0125, { bevel: 0 }), 0, 0, -0.00625);
    const point = [0, 0, 0];
    out.instanced.push({
      material: S,
      geometry: link,
      count,
      update(mesh, state) {
        const travel = state.crank * SPROCKET.crank;
        for (let i = 0; i < count; i += 1) {
          loop.at(i * pitch + travel, point);
          _matrix.makeRotationZ(point[2]);
          // Alternate inner and outer plates.
          if (i % 2) _matrix.scale(_vector.set(1, 1, 0.72));
          _matrix.setPosition(point[0], point[1], CHAIN_Z);
          mesh.setMatrixAt(i, _matrix);
        }
        mesh.instanceMatrix.needsUpdate = true;
      },
    });
  },

  *header(out) {
    out.local = true;
    const { bag } = out;
    const C = 'ceramic';
    const face = 0.1065;
    bag.add(C, move(rbox(0.008, 0.05, 0.452, 0.0028), face + 0.004, V0 + 0.027, 0));
    yield;
    const points = [-2, -1, 0, 1, 2].map((k) => [face + 0.008, V0 + 0.027, k * PITCH * 0.985]);
    boltRow(bag, 'steel', hexHead(0.012, 0.006), points, [1, 0, 0]);
    yield;
    // Four primaries sweep down and back into one collector (near-bank world space).
    const axis = [0.06, -0.86, -0.5];
    const length = Math.hypot(...axis);
    const a = axis.map((value) => value / length);
    const e1 = [0, 0.5, -0.86];
    const e2 = [a[1] * e1[2] - a[2] * e1[1], a[2] * e1[0] - a[0] * e1[2], a[0] * e1[1] - a[1] * e1[0]];
    const centre = [0.287, -0.04, -0.035];
    const slot = [[-1, 1], [-1, -1], [1, -1], [1, 1]]; // front pair then rear pair
    for (const [i, w] of W.entries()) {
      const start = bankPoint('near', face + 0.004, V0 + 0.027, w);
      const outboard = bankPoint('near', face + 0.052, V0 + 0.027, w);
      const end = centre.map((value, c) => value + (e1[c] * slot[i][0] + e2[c] * slot[i][1]) * 0.0215);
      const before = end.map((value, c) => value - a[c] * 0.07);
      const elbow = [outboard[0] + 0.012, 0.03, mix(start[2], end[2], 0.5)];
      const path = [start, outboard, elbow, before, end].map(toBank);
      bag.add(C, tube(path, 0.0205, { tubular: 18, radial: 12 }));
      yield;
    }
    // The collector is capped where the primaries enter, so nothing shows through.
    const collector = lathe(
      roundPoly(
        [[0, -0.024], [0.044, -0.024, 0.008], [0.052, -0.008, 0.004], [0.052, 0.012, 0.01], [0.037, 0.075, 0.01], [0.037, 0.112], [0.043, 0.112], [0.043, 0.119], [0.031, 0.119]],
        0,
        false,
      ),
      24,
    );
    const mouth = centre.map((value, c) => value + a[c] * 0.1194);
    bag.add(C, intoBank(place(collector, centre, a)));
    yield;
    bag.add(C, intoBank(place(disc(0.031, 16), mouth, a)), 0.05);
    yield;
  },

  *plugs(out) {
    out.local = true;
    const { bag } = out;
    const gasket = lathe([[0.011, 0], [0.011, 0.003], [0.008, 0.003]], 12);
    const shell = hexHead(0.016, 0.01);
    const insulator = lathe(
      [[0.0062, 0.016], [0.0069, 0.019], [0.0069, 0.022], [0.0057, 0.0245], [0.0064, 0.0275], [0.0057, 0.0305], [0.0064, 0.0335], [0.0052, 0.0365], [0.0052, 0.044], [0.0034, 0.044]],
      10,
    );
    const terminal = lathe([[0.0032, 0.044], [0.0032, 0.048], [0.004, 0.049], [0.004, 0.0525], [0, 0.0525]], 8);
    for (const w of W) {
      bag.add('steel', place(gasket.clone(), plugBase(w, 0.003), PLUG_AXIS));
      yield;
      bag.add('steel', place(shell.clone(), plugBase(w, 0.006), PLUG_AXIS));
      yield;
      bag.add('ceramic', place(insulator.clone(), plugBase(w, 0), PLUG_AXIS));
      yield;
      bag.add('steel', place(terminal.clone(), plugBase(w, 0), PLUG_AXIS));
      yield;
    }
  },

  *rockers(out) {
    out.local = true;
    const { bag } = out;
    const S = 'steel';
    const spring = helix(0.0145, 0.034, 4.5, 0.0021);
    const retainer = lathe([[0.0165, 0], [0.0165, 0.004], [0.006, 0.006], [0.004, 0.011], [0, 0.011]], 10);
    const nut = hexHead(0.014, 0.008);
    const stud = lathe([[0.004, 0], [0.004, 0.058]], 6);
    const pushrod = lathe([[0.0042, 0], [0.0042, 0.05]], 6);
    for (const centre of W) {
      for (const offset of [-0.0235, 0.0235]) {
        const w = centre + offset;
        bag.add(S, move(spring.clone(), 0.014, HEAD_TOP + 0.001, w), 0.85);
        yield;
        bag.add(S, move(retainer.clone(), 0.014, HEAD_TOP + 0.035, w));
        yield;
        const arm = rbox(0.054, 0.0095, 0.017, 0.003);
        arm.rotateZ(-0.14);
        bag.add(S, move(arm, -0.008, HEAD_TOP + 0.05, w));
        yield;
        bag.add(S, move(stud.clone(), -0.011, HEAD_TOP, w), 0.7);
        yield;
        bag.add(S, move(nut.clone(), -0.011, HEAD_TOP + 0.0555, w));
        yield;
        bag.add(S, move(pushrod.clone(), -0.034, HEAD_TOP - 0.002, w), 0.75);
        yield;
      }
    }
  },

  *pistons(out, { side, part }) {
    const bank = BANKS[side];
    const boreAngle = Math.atan2(bank.n[1], bank.n[0]);
    const piston = pistonGeometry();
    yield;
    const rod = rodGeometry();
    yield;
    const travel = [0, 0, 0];
    const poses = W.map(() => ({ x: 0, y: 0, z: 0, swing: 0 }));
    const solve = (state) => {
      partTravel(part, state.layout, travel);
      W.forEach((w, index) => {
        const theta = Math.PI / 2 + state.crank + PIN_PHASE[index] - boreAngle;
        const height = pistonPinHeight(theta, DIM.strokeRadius, DIM.rodLength);
        const amount = instanceAmount(part, index, state.p);
        const pose = poses[index];
        pose.x = bank.n[0] * height + travel[0] * amount;
        pose.y = bank.n[1] * height + travel[1] * amount;
        pose.z = w + bank.shift + travel[2] * amount;
        pose.swing = -rodSwing(theta, DIM.strokeRadius, DIM.rodLength);
      });
    };
    const write = (mesh, state, swinging) => {
      solve(state);
      poses.forEach((pose, i) => {
        _matrix
          .makeRotationZ(boreAngle - Math.PI / 2 + (swinging ? pose.swing : 0))
          .setPosition(pose.x, pose.y, pose.z);
        mesh.setMatrixAt(i, _matrix);
      });
      mesh.instanceMatrix.needsUpdate = true;
    };
    out.selfPosed = true;
    out.instanced.push(
      { material: 'castAluminium', geometry: piston, count: 4, update: (mesh, state) => write(mesh, state, false) },
      { material: 'steel', geometry: rod, count: 4, update: (mesh, state) => write(mesh, state, true) },
    );
    // Stand-in used for framing: the pistons at top dead centre.
    const top = DIM.strokeRadius + DIM.rodLength;
    out.envelope = W.map((w) =>
      move(piston.clone().rotateZ(boreAngle - Math.PI / 2), bank.n[0] * top, bank.n[1] * top, w + bank.shift),
    );
  },

  *crank(out) {
    const shaft = { center: [0, 0, 0], ratio: 1, bag: new Bag() };
    const S = 'steel';
    const journal = (radius, z0, z1, sides) =>
      alongZ(lathe([[radius - 0.0015, z0], [radius, z0 + 0.0015], [radius, z1 - 0.0015], [radius - 0.0015, z1]], sides));
    for (let k = -2; k <= 2; k += 1) shaft.bag.add(S, journal(0.031, k * PITCH - 0.016, k * PITCH + 0.016, 20));
    yield;
    for (const [index, phase] of PIN_PHASE.entries()) {
      const z = (1.5 - index) * PITCH;
      const angle = Math.PI / 2 + phase;
      const px = Math.cos(angle) * DIM.strokeRadius;
      const py = Math.sin(angle) * DIM.strokeRadius;
      shaft.bag.add(S, move(journal(0.0265, z - 0.0235, z + 0.0235, 16), px, py, 0));
      yield;
      for (const sign of [1, -1]) {
        const outer = (index === 0 && sign === 1) || (index === 3 && sign === -1);
        const reach = outer ? 0.09 : 0.076;
        const spread = outer ? 68 : 56;
        const cheek = roundPoly(
          [
            ...circle(0, DIM.strokeRadius, 0.034, 6, 15 * DEG, 165 * DEG).map((p) => [p[0], p[1], 0]),
            [-0.04, 0.012, 0.014],
            ...circle(0, 0, reach, 8, (270 - spread) * DEG, (270 + spread) * DEG).map((p, i, list) => [
              p[0],
              p[1],
              i === 0 || i === list.length - 1 ? 0.007 : 0,
            ]),
            [0.04, 0.012, 0.014],
          ],
          0,
        );
        const web = move(extrude(cheek, 0.0184, { bevel: 0.002 }), 0, 0, -0.0092);
        web.rotateZ(phase);
        shaft.bag.add(S, move(web, 0, 0, z + sign * (0.0225 + 0.0092)), 0.62);
        yield;
      }
    }
    const snout = lathe([[0, 0.2385], [0.024, 0.2385], [0.024, 0.3], [0.0185, 0.304], [0.0185, 0.3405], [0.017, 0.342], [0, 0.342]], 16);
    shaft.bag.add(S, alongZ(snout));
    yield;
    const flange = lathe([[0, -0.262], [0.0435, -0.262], [0.045, -0.2605], [0.045, -0.2385], [0, -0.2385]], 24);
    shaft.bag.add(S, alongZ(flange));
    yield;
    out.spinners.push(shaft);
  },

  *oil_pan({ bag }) {
    const top = -0.0072;
    const side = (inset) => {
      const i = inset;
      return roundPoly(
        [
          [0.2555 - i, top, 0],
          [0.2505 - i, -0.072 + i, 0.02],
          [0.02 + i * 0.4, -0.078 + i, 0.03],
          [-0.035 + i * 0.4, -0.186 + i, 0.022],
          [-0.238 + i, -0.186 + i, 0.022],
          [-0.2555 + i, -0.03, 0.03],
          [-0.2555 + i, top, 0],
        ],
        0.012,
        true,
        3,
      );
    };
    // An open sheet-metal pan: the outer shell and an inside-out tub, both topless.
    const open = (x, y, z, nx, ny) => ny > 0.7 && y > top - 0.004;
    const shell = dropFaces(extrudeAcross(side(0), -0.102, 0.102, { bevel: 0.011, segments: 3 }), open);
    bag.add('blackSatin', shell);
    yield;
    const tub = dropFaces(extrudeAcross(side(0.004), -0.098, 0.098, { bevel: 0.009, segments: 2 }), open);
    flip(tub, true);
    shade(tub, (x, y) => mix(0.16, 0.6, smooth(-0.15, top, y)));
    bag.add('blackSatin', tub);
    yield;
    const rail = roundRect(-0.126, -ZF - 0.001, 0.126, ZF + 0.001, 0.012);
    const mouth = roundRect(-0.098, -ZF + 0.008, 0.098, ZF - 0.008, 0.012);
    bag.add('blackSatin', extrudeUp(rail, -0.0096, -0.0052, { bevel: 0.0012, holes: [mouth] }));
    yield;
    for (const x of [-0.113, 0.113]) {
      for (let i = 0; i < 7; i += 1) {
        bag.add('blackSatin', move(disc(0.0042, 8), x, -0.005, -0.222 + i * 0.074), 0.12);
        yield;
      }
    }
    bag.add('steel', place(hexHead(0.019, 0.009, true), [0.102, -0.158, -0.19], [1, 0, 0]));
    yield;
  },

  *oil_filter({ bag }) {
    const can = lathe(
      roundPoly(
        [[0, -0.118], [0.021, -0.118, 0.004], [0.034, -0.112, 0.008], [0.038, -0.099, 0.008], [0.038, -0.024, 0.002], [0.0366, -0.0195], [0.0366, -0.0135, 0.002], [0.03, -0.011], [0, -0.011]],
        0,
        false,
      ),
      44,
    );
    bag.add('yellow', move(can, 0.15, 0, -0.175));
    yield;
    // Wrench flats around the closed end: a faceted band, kept hard-edged.
    const grip = lathe([[0.0372, -0.111], [0.0392, -0.108], [0.0392, -0.097], [0.0372, -0.094]], 14, HARD);
    bag.add('yellow', move(grip, 0.15, 0, -0.175), 0.85);
    yield;
    const seal = lathe([[0.031, 0], [0.036, 0], [0.036, 0.0052], [0.031, 0.0052]], 24);
    bag.add('rubber', move(seal, 0.15, -0.0112, -0.175));
    yield;
  },

  *flywheel(out) {
    const wheel = { center: [0, 0, 0], ratio: 1, bag: new Bag() };
    const S = 'steel';
    const plate = lathe(
      [[0, -0.292], [0.165, -0.292], [0.171, -0.288], [0.171, -0.27], [0.165, -0.266], [0.07, -0.264], [0.045, -0.2625], [0, -0.2625]],
      48,
    );
    wheel.bag.add(S, alongZ(plate), 0.78);
    yield;
    const teeth = extrude(gearOutline(96, 0.1702, 0.1776, 0.26), 0.014, { bevel: 0, holes: [circle(0, 0, 0.168, 48)] });
    wheel.bag.add(S, move(teeth, 0, 0, -0.286), 0.66);
    yield;
    for (let i = 0; i < 6; i += 1) {
      const angle = (i / 6) * TAU;
      wheel.bag.add(S, place(disc(0.013, 14), [Math.cos(angle) * 0.108, Math.sin(angle) * 0.108, -0.2644], [0, 0, 1]), 0.05);
      yield;
    }
    out.spinners.push(wheel);
    // Starter motor tucked under the far bank.
    const { bag } = out;
    const motor = lathe(roundPoly([[0, -0.25], [0.04, -0.25, 0.004], [0.04, -0.1, 0.006], [0.034, -0.092], [0, -0.092]], 0, false), 24);
    bag.add('blackSatin', move(alongZ(motor), -0.152, -0.088, 0));
    yield;
    const nose = lathe([[0, -0.292], [0.03, -0.292], [0.036, -0.252], [0, -0.252]], 20);
    bag.add('castAluminium', move(alongZ(nose), -0.152, -0.088, 0));
    yield;
    const solenoid = lathe(roundPoly([[0, -0.235], [0.022, -0.235, 0.003], [0.022, -0.125, 0.003], [0, -0.125]], 0, false), 16);
    bag.add('steel', move(alongZ(solenoid), -0.176, -0.036, 0), 0.8);
    yield;
  },
};

// ----------------------------------------------------------------------------
// Assembly
// ----------------------------------------------------------------------------

/** Array offsets handled per step when a pass walks every vertex of a part. */
const CHUNK = 3 * 6000;

const SUPPORT = (() => {
  const out = [];
  for (let x = -1; x <= 1; x += 1) {
    for (let y = -1; y <= 1; y += 1) {
      for (let z = -1; z <= 1; z += 1) {
        const length = Math.hypot(x, y, z);
        if (length > 0) out.push([x / length, y / length, z / length]);
      }
    }
  }
  return out;
})();

/**
 * The extreme points of some geometry in 26 directions: a cheap convex stand-in,
 * gathered one geometry at a time.
 */
function createSupport() {
  const best = new Float64Array(SUPPORT.length).fill(-Infinity);
  const points = new Float32Array(SUPPORT.length * 3);
  return {
    points,
    add(geometry, from = 0, to = Infinity) {
      const position = geometry.attributes.position.array;
      const end = Math.min(to, position.length);
      for (let d = 0; d < SUPPORT.length; d += 1) {
        const dx = SUPPORT[d][0];
        const dy = SUPPORT[d][1];
        const dz = SUPPORT[d][2];
        let reach = best[d];
        let at = -1;
        for (let i = from; i < end; i += 3) {
          const along = position[i] * dx + position[i + 1] * dy + position[i + 2] * dz;
          if (along > reach) {
            reach = along;
            at = i;
          }
        }
        if (at >= 0) {
          best[d] = reach;
          points[d * 3] = position[at];
          points[d * 3 + 1] = position[at + 1];
          points[d * 3 + 2] = position[at + 2];
        }
      }
    },
  };
}

function packColour(geometry) {
  const colour = geometry.attributes.color.array;
  const bytes = new Uint8Array(colour.length);
  for (let i = 0; i < colour.length; i += 1) bytes[i] = Math.round(clamp01(colour[i]) * 255);
  geometry.setAttribute('color', new BufferAttribute(bytes, 3, true));
  geometry.computeBoundingSphere();
  return geometry;
}

/**
 * Run each kind of primitive once, tiny, one per step.
 *
 * The first call of anything pays for compiling it. Spent here, a little at a
 * time, that cost never lands inside the first real primitive of the build.
 */
export function* prime() {
  detail = 1;
  const outline = roundRect(-0.01, -0.01, 0.01, 0.01, 0.003);
  const pieces = [extrude(outline, 0.01, { holes: [circle(0, 0, 0.004, 8)] })];
  yield 'prime';
  pieces.push(extrudeUp(outline, 0, 0.01, { bevel: 0 }));
  yield 'prime';
  pieces.push(rbox(0.01, 0.01, 0.01, 0.002), box(0.01, 0.01, 0.01));
  yield 'prime';
  pieces.push(lathe([[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01]], 8), disc(0.01, 8));
  yield 'prime';
  pieces.push(tube([[0, 0, 0], [0.01, 0, 0], [0.02, 0.01, 0]], 0.002, { tubular: 4, radial: 6 }));
  yield 'prime';
  for (const geometry of pieces) transform(geometry, bankMatrix('near'));
  const merged = mergeGeometries(pieces, false);
  yield 'prime';
  createSupport().add(merged);
  yield 'prime';
  castOcclusion(merged, 'prime');
  yield 'prime';
  packColour(merged);
  yield 'prime';
}

/**
 * Build one part from the parts table, as a generator. `layout` picks the
 * detail tier.
 *
 * Every `yield` is a checkpoint between two small pieces of work, so the caller
 * can hand the main thread back whenever its time slice is spent: nothing in
 * here runs for long in one go. The generator's return value has `meshes`
 * ({ material, geometry }), `spinners` (sub-assemblies that turn with the crank:
 * { center, ratio, meshes }), `instanced` ({ material, geometry, count,
 * update(mesh, state) }), `selfPosed` (instances carry the explode offset
 * themselves) and `support` (points used to frame the camera).
 */
export function* buildPart(part, layout = 'landscape') {
  const level = layout === 'portrait' ? 0.75 : 1;
  const match = /^(.*)_(near|far)$/.exec(part.id);
  const name = match ? match[1] : part.id;
  const side = match ? match[2] : null;
  const out = { bag: new Bag(), spinners: [], instanced: [], selfPosed: false, local: false, envelope: null };
  const pieces = BUILDERS[name](out, { side, part });
  for (;;) {
    // Set on every resume: another build may have run in between.
    detail = level;
    if (pieces.next().done) break;
    yield 'shape';
  }
  if (out.local) {
    const matrix = bankMatrix(side);
    for (const list of out.bag.lists.values()) {
      for (const geometry of list) transform(geometry, matrix);
      yield 'place';
    }
  }

  const support = createSupport();
  function* finished(bag, centre) {
    const list = [];
    for (const [material, parts] of bag.lists) {
      const geometry = parts.length === 1 ? parts[0] : mergeGeometries(parts, false);
      yield 'merge';
      // Framing uses the part where it sits, before a spinner is recentred on its axis.
      const size = geometry.attributes.position.array.length;
      for (let from = 0; from < size; from += CHUNK) {
        support.add(geometry, from, from + CHUNK);
        yield 'frame';
        castOcclusion(geometry, part.id, from, from + CHUNK);
        yield 'occlusion';
      }
      if (centre) geometry.translate(-centre[0], -centre[1], -centre[2]);
      list.push({ material, geometry: packColour(geometry) });
      yield 'pack';
    }
    return list;
  }

  const meshes = yield* finished(out.bag, null);
  const spinners = [];
  for (const spinner of out.spinners) {
    spinners.push({
      center: spinner.center,
      ratio: spinner.ratio,
      meshes: yield* finished(spinner.bag, spinner.center),
    });
  }
  for (const item of out.instanced) packColour(item.geometry);
  if (out.envelope) {
    for (const geometry of out.envelope) support.add(geometry);
  }
  yield 'frame';
  return { meshes, spinners, instanced: out.instanced, selfPosed: out.selfPosed, support: support.points };
}
