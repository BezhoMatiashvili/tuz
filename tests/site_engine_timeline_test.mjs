// Timeline tests for the engine section of the public site.
//
//   node tests/site_engine_timeline_test.mjs
//
// The timeline and the parts table are pure modules with no three.js and no
// DOM, so plain Node can exercise the whole choreography. The pacer that cuts
// the 3D start-up into small tasks (pace.js) is pure too and is tested here.

import assert from 'node:assert/strict';

import { SLICE_MS, createPacer } from '../automotive/static/site/js/engine/pace.js';
import { BANKS, DIM, PARTS, partForKey, partsFor } from '../automotive/static/site/js/engine/parts.js';
import {
  BEAT_STARTS,
  CAMERA,
  CRANK_TURNS,
  HYSTERESIS,
  LAYOUT_SCALE,
  PHASE,
  beatAt,
  buildCameraTrack,
  cameraAt,
  cameraTrackSteps,
  crankAngle,
  cubicBezier,
  easeInOut,
  fitView,
  frameWindow,
  instanceAmount,
  partAmount,
  partMoves,
  partOffset,
  partTravel,
  pinsAt,
  pistonPinHeight,
  posterAt,
  rodSwing,
  scrollProgress,
  seatAmount,
  smoothProgress,
  viewBasis,
} from '../automotive/static/site/js/engine/timeline.js';

const LAYOUTS = ['landscape', 'portrait'];
const length = (v) => Math.hypot(v[0], v[1], v[2]);
const near = (actual, expected, tolerance = 1e-9, message = '') =>
  assert.ok(Math.abs(actual - expected) <= tolerance, `${message} expected ${expected}, got ${actual}`);

let passed = 0;
const failures = [];
function failed(name, error) {
  failures.push(`${name}\n    ${String(error.message).split('\n').join('\n    ')}`);
}

/** Runs `body`; when it is async, returns a promise to await before the next test. */
function test(name, body) {
  try {
    const result = body();
    if (result && typeof result.then === 'function') {
      return result.then(
        () => {
          passed += 1;
        },
        (error) => failed(name, error),
      );
    }
    passed += 1;
  } catch (error) {
    failed(name, error);
  }
  return undefined;
}

// ---------------------------------------------------------------- parts table

test('part ids are unique and every window sits inside the explode phase', () => {
  const ids = new Set();
  for (const part of PARTS) {
    assert.ok(!ids.has(part.id), `duplicate id ${part.id}`);
    ids.add(part.id);
    if (!part.window) continue;
    const [start, end] = part.window;
    assert.ok(start >= PHASE.explodeStart - 1e-9, `${part.id} starts before the explode phase`);
    assert.ok(end <= PHASE.explodeEnd + 1e-9, `${part.id} ends after the explode phase`);
    assert.ok(end > start, `${part.id} has an empty window`);
  }
  assert.equal(PARTS.filter((part) => !part.window).map((part) => part.id).join(), 'block');
});

test('a part only rides a part that is built in every layout it is built in', () => {
  for (const layout of LAYOUTS) {
    const built = new Set(partsFor(layout));
    for (const part of built) {
      if (part.rides) assert.ok(built.has(part.rides), `${part.id} rides a missing part in ${layout}`);
    }
  }
});

test('legend keys resolve to a part in both layouts', () => {
  const keys = ['air_filter', 'spark_plugs', 'oil_filter', 'timing_kit', 'engine_oil', 'gasket', 'coolant', 'belt'];
  for (const key of keys) {
    assert.ok(partForKey(key, 'landscape'), `${key} has no part on desktop`);
  }
  for (const key of keys.slice(0, 5)) {
    const part = partForKey(key, 'portrait');
    assert.ok(part, `${key} has no part on phones`);
    assert.ok(part.anchor, `${key} has no pin anchor on phones`);
  }
  assert.equal(partForKey('nonsense', 'landscape'), null);
});

test('phones build fewer parts and keep the far bank as an anchor', () => {
  const phone = partsFor('portrait');
  assert.ok(phone.length < PARTS.length);
  assert.ok(phone.some((part) => part.id === 'head_far'));
  assert.ok(!phone.some((part) => part.id === 'plug_wires'));
  for (const part of phone) {
    if (part.bank === 'far') assert.equal(partMoves(part, 'portrait'), false, part.id);
  }
});

// ------------------------------------------------------------------ rest pose

test('every part is at rest at p = 0', () => {
  for (const layout of LAYOUTS) {
    for (const part of PARTS) {
      assert.deepEqual(partOffset(part, 0, layout), [0, 0, 0], `${part.id} in ${layout}`);
      assert.equal(partAmount(part, 0), 0);
      if (part.stagger) {
        part.stagger.forEach((_, index) => assert.equal(instanceAmount(part, index, 0), 0));
      }
    }
  }
});

test('nothing has moved when the explode phase begins', () => {
  for (const layout of LAYOUTS) {
    for (const part of PARTS) {
      assert.equal(length(partOffset(part, PHASE.explodeStart, layout)), 0, part.id);
    }
  }
});

// --------------------------------------------------------------- pure posing

test('poses are a pure function of p', () => {
  const samples = [0.13, 0.31, 0.5, 0.67, 0.9];
  for (const layout of LAYOUTS) {
    for (const part of PARTS) {
      const first = samples.map((p) => partOffset(part, p, layout));
      // Visit other progress values in between, in both directions.
      for (let p = 1; p >= 0; p -= 0.07) partOffset(part, p, layout);
      const second = samples.map((p) => partOffset(part, p, layout));
      assert.deepEqual(second, first, `${part.id} in ${layout}`);
    }
  }
});

test('an output array passed in is filled, not accumulated into', () => {
  const part = PARTS.find((item) => item.id === 'plugs_near');
  const out = [9, 9, 9];
  partOffset(part, 0.5, 'landscape', out);
  assert.deepEqual(out, partOffset(part, 0.5, 'landscape'));
});

test('distance from the rest pose never decreases as p grows', () => {
  for (const layout of LAYOUTS) {
    for (const part of PARTS) {
      let previous = 0;
      for (let step = 0; step <= 400; step += 1) {
        const distance = length(partOffset(part, step / 400, layout));
        assert.ok(distance >= previous - 1e-12, `${part.id} moved back at p=${step / 400} in ${layout}`);
        previous = distance;
      }
    }
  }
});

test('each part travels only inside its window and is done when the hold begins', () => {
  for (const layout of LAYOUTS) {
    for (const part of PARTS) {
      if (!part.window) continue;
      const [start, end] = part.window;
      assert.equal(partAmount(part, start), 0, part.id);
      assert.equal(partAmount(part, end), 1, part.id);
      assert.deepEqual(
        partOffset(part, PHASE.explodeEnd, layout),
        partOffset(part, 1, layout),
        `${part.id} still moving during the hold in ${layout}`,
      );
    }
  }
});

test('staggered instances all leave and all arrive inside the window', () => {
  for (const part of PARTS.filter((item) => item.stagger)) {
    const [start, end] = part.window;
    part.stagger.forEach((delay, index) => {
      near(instanceAmount(part, index, start + delay), 0, 1e-12);
      assert.equal(instanceAmount(part, index, end), 1);
      let previous = 0;
      for (let step = 0; step <= 200; step += 1) {
        const amount = instanceAmount(part, index, step / 200);
        assert.ok(amount >= previous - 1e-12);
        previous = amount;
      }
    });
  }
});

// ------------------------------------------------------------- layout scaling

test('portrait multipliers are applied per axis', () => {
  const scale = LAYOUT_SCALE.portrait;
  assert.deepEqual(LAYOUT_SCALE.landscape, [1, 1, 1]);
  for (const id of ['oil_pan', 'belt', 'alternator', 'oil_filter', 'intake']) {
    const part = PARTS.find((item) => item.id === id);
    const wide = partTravel(part, 'landscape');
    const tall = partTravel(part, 'portrait');
    assert.deepEqual(wide, [...part.explode], id);
    for (let axis = 0; axis < 3; axis += 1) {
      near(tall[axis], wide[axis] * scale[axis], 1e-12, `${id} axis ${axis}`);
    }
    for (const p of [0.3, 0.45, 0.8]) {
      const a = partOffset(part, p, 'landscape');
      const b = partOffset(part, p, 'portrait');
      for (let axis = 0; axis < 3; axis += 1) near(b[axis], a[axis] * scale[axis], 1e-12, id);
    }
  }
});

test('an unknown layout falls back to landscape', () => {
  const part = PARTS.find((item) => item.id === 'belt');
  assert.deepEqual(partOffset(part, 0.9, 'nonsense'), partOffset(part, 0.9, 'landscape'));
});

test('pistons leave along their bore in every layout', () => {
  for (const side of ['near', 'far']) {
    const part = PARTS.find((item) => item.id === `pistons_${side}`);
    assert.equal(part.rigid, true);
    const travel = partTravel(part, 'landscape');
    const along = travel[0] * BANKS[side].n[0] + travel[1] * BANKS[side].n[1];
    near(along, length(travel), 1e-12, 'travel is along the bank normal');
    near(travel[0] * BANKS[side].t[0] + travel[1] * BANKS[side].t[1], 0, 1e-12);
  }
  const nearPistons = PARTS.find((item) => item.id === 'pistons_near');
  assert.deepEqual(partTravel(nearPistons, 'portrait'), partTravel(nearPistons, 'landscape'));
});

test('a riding part is carried by the part it sits in', () => {
  const head = PARTS.find((item) => item.id === 'head_near');
  const plugs = PARTS.find((item) => item.id === 'plugs_near');
  const rockers = PARTS.find((item) => item.id === 'rockers_near');
  assert.equal(plugs.rides, head);
  for (const layout of LAYOUTS) {
    const carried = partOffset(head, 1, layout);
    assert.deepEqual(partOffset(rockers, 1, layout), carried, 'rockers stay on the head');
    const own = partTravel(plugs, layout);
    const total = partOffset(plugs, 1, layout);
    for (let axis = 0; axis < 3; axis += 1) near(total[axis], carried[axis] + own[axis], 1e-12);
    // The plugs pull out of the head before the head itself leaves the block.
    assert.ok(length(partOffset(plugs, 0.33, layout)) > 0);
    assert.equal(length(partOffset(head, 0.33, layout)), 0);
  }
  const lid = PARTS.find((item) => item.id === 'air_lid');
  let chain = 0;
  for (let part = lid; part; part = part.rides) chain += part.explode[1];
  near(partOffset(lid, 1, 'landscape')[1], chain, 1e-12, 'the induction stack adds up');
});

// ------------------------------------------------------------------- easing

test('the cubic-bezier solver hits its endpoints', () => {
  const curves = [
    [0.77, 0, 0.175, 1],
    [0.23, 1, 0.32, 1],
    [0.25, 0.1, 0.25, 1],
    [0, 0, 1, 1],
    [0.42, 0, 0.58, 1],
    [0.9, 0, 0.1, 1],
  ];
  for (const [x1, y1, x2, y2] of curves) {
    const ease = cubicBezier(x1, y1, x2, y2);
    assert.equal(ease(0), 0);
    assert.equal(ease(1), 1);
    assert.equal(ease(-0.5), 0);
    assert.equal(ease(1.5), 1);
    near(ease(1e-9), 0, 1e-6);
    near(ease(1 - 1e-9), 1, 1e-6);
  }
});

test('the cubic-bezier solver matches known values and is monotonic', () => {
  const linear = cubicBezier(0, 0, 1, 1);
  for (const x of [0.1, 0.25, 0.5, 0.9]) near(linear(x), x, 1e-6);
  near(cubicBezier(0.25, 0.1, 0.25, 1)(0.5), 0.8024034, 1e-5, 'CSS ease at 0.5');
  near(cubicBezier(0.42, 0, 0.58, 1)(0.5), 0.5, 1e-6, 'CSS ease-in-out is symmetric');
  let previous = 0;
  for (let step = 0; step <= 1000; step += 1) {
    const value = easeInOut(step / 1000);
    assert.ok(value >= previous - 1e-9, `easeInOut dips at ${step / 1000}`);
    assert.ok(value >= 0 && value <= 1);
    previous = value;
  }
});

// -------------------------------------------------------------------- beats

test('beats start where the copy says they do', () => {
  assert.deepEqual([...BEAT_STARTS], [0.22, 0.68]);
  assert.equal(beatAt(0), 0);
  assert.equal(beatAt(0.2199), 0);
  assert.equal(beatAt(0.22), 1);
  assert.equal(beatAt(0.6799), 1);
  assert.equal(beatAt(0.68), 2);
  assert.equal(beatAt(1), 2);
});

test('beat selection has hysteresis in both directions', () => {
  const edge = BEAT_STARTS[0];
  assert.equal(beatAt(edge + HYSTERESIS / 2, 0), 0, 'not yet past the upper band');
  assert.equal(beatAt(edge + HYSTERESIS, 0), 1);
  assert.equal(beatAt(edge - HYSTERESIS / 2, 1), 1, 'not yet past the lower band');
  assert.equal(beatAt(edge - HYSTERESIS - 1e-6, 1), 0);
  assert.equal(beatAt(0.69, 1), 1);
  assert.equal(beatAt(0.701, 1), 2);
  assert.equal(beatAt(0.67, 2), 2);
  assert.equal(beatAt(0.659, 2), 1);
  assert.equal(beatAt(0.9, 0), 2, 'a fling skips a beat');
  assert.equal(beatAt(0.05, 2), 0);
});

test('a finger resting on a boundary cannot make the copy flicker', () => {
  for (const edge of BEAT_STARTS) {
    let beat = beatAt(edge - 0.05);
    let changes = 0;
    for (let step = 0; step < 200; step += 1) {
      const p = edge + Math.sin(step * 1.7) * (HYSTERESIS * 0.9);
      const next = beatAt(p, beat);
      if (next !== beat) changes += 1;
      beat = next;
    }
    assert.equal(changes, 0, `flicker around ${edge}`);
  }
});

test('poster and pin thresholds behave like beats', () => {
  assert.equal(posterAt(0.39), 0);
  assert.equal(posterAt(0.4), 1);
  assert.equal(posterAt(0.41, 0), 0);
  assert.equal(posterAt(0.43, 0), 1);
  assert.equal(posterAt(0.39, 1), 1);
  assert.equal(pinsAt(0.5), 0);
  assert.equal(pinsAt(0.8), 1);
});

// ------------------------------------------------------- progress and motion

test('scroll progress is clamped and linear over the sticky travel', () => {
  const section = 3000;
  const stage = 800;
  const sticky = 56;
  assert.equal(scrollProgress(500, section, stage, sticky), 0);
  assert.equal(scrollProgress(sticky, section, stage, sticky), 0);
  near(scrollProgress(sticky - 1100, section, stage, sticky), 0.5);
  assert.equal(scrollProgress(sticky - 2200, section, stage, sticky), 1);
  assert.equal(scrollProgress(-9000, section, stage, sticky), 1);
  assert.equal(scrollProgress(0, 800, 800, sticky), 0, 'no travel, no progress');
});

test('smoothing approaches the target, limits the lag and snaps', () => {
  near(smoothProgress(0.2, 0.2, 16), 0.2);
  const step = smoothProgress(0.2, 0.23, 16);
  assert.ok(step > 0.2 && step < 0.23);
  near(smoothProgress(0, 1, 16), 0.94, 1e-12, 'lag is capped at 0.06');
  near(smoothProgress(1, 0, 16), 0.06, 1e-12);
  assert.equal(smoothProgress(0.5, 0.5003, 16), 0.5003, 'snaps when close');
  assert.equal(smoothProgress(0.4, 0.45, 0), 0.4, 'no time, no movement');
  let value = 0.3;
  for (let frame = 0; frame < 120; frame += 1) value = smoothProgress(value, 0.34, 16.7);
  assert.equal(value, 0.34);
});

test('the crank turns clockwise and the pistons follow it', () => {
  assert.equal(crankAngle(0), 0);
  near(crankAngle(1), -CRANK_TURNS * 2 * Math.PI);
  assert.ok(crankAngle(0.3) < 0);
  const { strokeRadius: r, rodLength: l } = DIM;
  near(pistonPinHeight(0, r, l), r + l);
  near(pistonPinHeight(Math.PI, r, l), l - r);
  near(pistonPinHeight(0, r, l) - pistonPinHeight(Math.PI, r, l), 2 * r, 1e-12, 'stroke');
  near(rodSwing(0, r, l), 0);
  near(rodSwing(Math.PI / 2, r, l), Math.asin(r / l));
  near(rodSwing(-Math.PI / 2, r, l), -Math.asin(r / l));
});

test('parts are seated at rest and loose once the engine is apart', () => {
  assert.equal(seatAmount(0), 1);
  assert.equal(seatAmount(PHASE.explodeStart), 1);
  assert.equal(seatAmount(PHASE.explodeEnd), 0);
  assert.equal(seatAmount(1), 0);
});

// ------------------------------------------------------------------- camera

function cloud(p) {
  // A box that grows taller and wider with p, like the exploding engine.
  const half = [0.35 + 0.5 * p, 0.35 + 0.6 * p, 0.35 + 0.4 * p];
  const points = [];
  for (const x of [-1, 1]) {
    for (const y of [-1, 1]) {
      for (const z of [-1, 1]) points.push(x * half[0], y * half[1] + 0.1, z * half[2]);
    }
  }
  return { points, count: 8 };
}

function project(point, camera, basis, window) {
  const rel = point.map((value, axis) => value - camera[axis]);
  const depth = -(rel[0] * basis.k[0] + rel[1] * basis.k[1] + rel[2] * basis.k[2]);
  const x = (rel[0] * basis.r[0] + rel[1] * basis.r[1] + rel[2] * basis.r[2]) / depth;
  const y = (rel[0] * basis.u[0] + rel[1] * basis.u[1] + rel[2] * basis.u[2]) / depth;
  return [(x - window.left) / (window.right - window.left), (window.top - y) / (window.top - window.bottom)];
}

test('the view basis is orthonormal', () => {
  const { k, r, u } = viewBasis(0.8, 0.25);
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  for (const axis of [k, r, u]) near(dot(axis, axis), 1, 1e-12);
  near(dot(k, r), 0, 1e-12);
  near(dot(k, u), 0, 1e-12);
  near(dot(r, u), 0, 1e-12);
  assert.ok(u[1] > 0, 'up points up');
});

test('fitView frames a point cloud tightly and centred', () => {
  const basis = viewBasis(0.75, 0.3);
  const { points, count } = cloud(0.4);
  const halfX = 0.3;
  const halfY = 0.2;
  const fit = fitView(points, count, basis, halfX, halfY);
  let left = Infinity;
  let right = -Infinity;
  let bottom = Infinity;
  let top = -Infinity;
  for (let i = 0; i < count; i += 1) {
    const [x, y, z] = [points[3 * i], points[3 * i + 1], points[3 * i + 2]];
    const depth = fit.distance - (x * basis.k[0] + y * basis.k[1] + z * basis.k[2]);
    assert.ok(depth > 0, 'every point is in front of the camera');
    const a = (x * basis.r[0] + y * basis.r[1] + z * basis.r[2] - fit.shiftRight) / depth;
    const b = (x * basis.u[0] + y * basis.u[1] + z * basis.u[2] - fit.shiftUp) / depth;
    left = Math.min(left, a);
    right = Math.max(right, a);
    bottom = Math.min(bottom, b);
    top = Math.max(top, b);
  }
  assert.ok(right <= halfX + 1e-4 && -left <= halfX + 1e-4, 'fits horizontally');
  assert.ok(top <= halfY + 1e-4 && -bottom <= halfY + 1e-4, 'fits vertically');
  near(right, -left, 1e-4, 'centred horizontally');
  near(top, -bottom, 1e-4, 'centred vertically');
  const tight = Math.max(right / halfX, top / halfY);
  assert.ok(tight > 0.97, `the limiting axis is nearly full (${tight})`);
});

test('the frame window leaves the reserved part of the frame free', () => {
  const full = frameWindow(1.6);
  near(full.right, -full.left);
  near(full.top, -full.bottom);
  near(full.halfX / full.halfY, 1.6);
  near(full.top, Math.tan((CAMERA.fov * Math.PI) / 360));
  const shifted = frameWindow(1.6, [0.38, 0, 1, 1]);
  near(shifted.right - shifted.left, full.right - full.left, 1e-12, 'same field of view');
  near(shifted.halfX, full.halfX * 0.62);
  // The camera axis sits in the middle of the safe area: 69% across the frame.
  near(-shifted.left / (shifted.right - shifted.left), 0.69);
});

test('the camera track keeps the model inside the safe area for the whole scroll', () => {
  for (const [aspect, safe] of [
    [2 / 3, [0, 0, 1, 1]],
    [1.6, [0.38, 0, 1, 1]],
  ]) {
    const track = buildCameraTrack(cloud, aspect, { safe });
    let previous = 0;
    for (let step = 0; step <= 100; step += 1) {
      const p = step / 100;
      const view = cameraAt(track, p);
      assert.ok(view.distance >= previous - 1e-9, `camera moved in at p=${p}`);
      previous = view.distance;
      const basis = viewBasis(view.azimuth, view.elevation);
      const { points, count } = cloud(p);
      for (let i = 0; i < count; i += 1) {
        const [x, y] = project(points.slice(3 * i, 3 * i + 3), view.position, basis, track.window);
        assert.ok(x >= safe[0] - 0.002 && x <= safe[2] + 0.002, `x=${x} leaves the safe area at p=${p}`);
        assert.ok(y >= safe[1] - 0.002 && y <= safe[3] + 0.002, `y=${y} leaves the frame at p=${p}`);
      }
    }
    assert.deepEqual(cameraAt(track, 0.37), cameraAt(track, 0.37), 'the camera is a pure function of p');
  }
});

test('the camera sweeps between the documented angles', () => {
  const track = buildCameraTrack(cloud, 1.6);
  const degrees = (radians) => (radians * 180) / Math.PI;
  near(degrees(cameraAt(track, 0).azimuth), CAMERA.azimuth[0], 1e-9);
  near(degrees(cameraAt(track, 1).azimuth), CAMERA.azimuth[1], 1e-9);
  near(degrees(cameraAt(track, 0).elevation), CAMERA.elevation[0], 1e-9);
  near(degrees(cameraAt(track, 1).elevation), CAMERA.elevation[1], 1e-9);
});

test('the camera track built in steps is the track built in one go', () => {
  const whole = buildCameraTrack(cloud, 1.6, { safe: [0.38, 0, 1, 1] });
  const steps = cameraTrackSteps(cloud, 1.6, { safe: [0.38, 0, 1, 1] });
  let count = 0;
  let step = steps.next();
  while (!step.done) {
    count += 1;
    step = steps.next();
  }
  assert.equal(count, whole.steps + 1, 'one pause per sample of the scroll');
  assert.deepEqual(step.value, whole);
});

// -----------------------------------------------------------------------------
// The pacer. Node has no scheduler.yield: stand one in that counts the pauses.

let pauses = 0;
globalThis.scheduler = {
  yield() {
    pauses += 1;
    return new Promise((resolve) => {
      setImmediate(resolve);
    });
  },
};

function* count(to, log) {
  for (let i = 0; i < to; i += 1) {
    log.push(i);
    yield i % 2 ? 'odd' : 'even';
  }
  return 'done';
}

await test('the pacer runs a generator to its end and returns its value', async () => {
  const log = [];
  const pacer = createPacer(null, 1e9);
  pauses = 0;
  assert.equal(await pacer.run(count(50, log)), 'done');
  assert.equal(log.length, 50);
  assert.equal(pauses, 0, 'nothing pauses while the slice has budget left');
});

await test('the pacer hands the thread back whenever a slice is used up', async () => {
  const log = [];
  const pacer = createPacer(null, 0);
  pauses = 0;
  // A task that is already waiting when the run starts: it must get its turn
  // at the first pause, before the second step.
  let seenBy = -1;
  setImmediate(() => {
    seenBy = log.length;
  });
  await pacer.run(count(20, log));
  assert.deepEqual(log, Array.from({ length: 20 }, (_, i) => i), 'steps stay in order');
  assert.ok(pauses >= 20, `a pause before every step, got ${pauses}`);
  assert.equal(seenBy, 0, 'a waiting task runs at the first pause');
  assert.equal(pacer.report.slices, pauses, 'every pause closes a slice');
  assert.ok(pacer.report.busy >= 0 && pacer.report.longest <= pacer.report.busy + 1e-9);
});

await test('the pacer names the longest step by stage and label', async () => {
  const pacer = createPacer(null, 1e9);
  pacer.mark('part:block');
  function* uneven() {
    yield 'quick';
    const until = performance.now() + 5;
    while (performance.now() < until);
    yield 'slow';
    yield 'quick';
  }
  await pacer.run(uneven());
  assert.equal(pacer.stage(), 'part:block');
  assert.equal(pacer.report.stepAt, 'part:block:slow');
  assert.ok(pacer.report.step >= 4.5, `the slow step took ${pacer.report.step} ms`);
});

await test('an aborted build stops at the next pause', async () => {
  const controller = new AbortController();
  const log = [];
  const pacer = createPacer(controller.signal, 0);
  function* steps() {
    for (let i = 0; i < 10; i += 1) {
      log.push(i);
      if (i === 3) controller.abort();
      yield;
    }
  }
  await assert.rejects(pacer.run(steps()), { name: 'AbortError' });
  assert.deepEqual(log, [0, 1, 2, 3], 'nothing runs after the abort');
  await assert.rejects(pacer.rest(), { name: 'AbortError' });
  await assert.rejects(pacer.wait(Promise.resolve(1)), { name: 'AbortError' });
});

await test('waiting on something else is not counted as work and resumes in a later task', async () => {
  const pacer = createPacer(null);
  let later = false;
  setImmediate(() => {
    later = true;
  });
  const before = pacer.report.busy;
  const value = await pacer.wait(
    new Promise((resolve) => {
      setTimeout(() => resolve('module'), 30);
    }),
  );
  assert.equal(value, 'module');
  assert.ok(later, 'the continuation runs after tasks that were already queued');
  assert.ok(pacer.report.busy - before < 20, `the 30 ms wait counted as ${pacer.report.busy - before} ms of work`);
  assert.ok(SLICE_MS > 0 && SLICE_MS <= 8, 'a slice leaves most of a 16 ms frame to the page');
});

// -----------------------------------------------------------------------------

if (failures.length) {
  console.error(`${failures.length} failed, ${passed} passed\n`);
  for (const failure of failures) console.error(`not ok  ${failure}\n`);
  process.exit(1);
}
console.log(`ok  ${passed} timeline and pacing tests`);
