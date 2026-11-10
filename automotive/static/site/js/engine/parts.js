/**
 * The engine parts table, as data.
 *
 * Units are metres. X is viewer-right, Y is up, Z points at the viewer (the front
 * of the engine, where the belt runs). The "near" bank is the one on +X.
 *
 * No three.js here: the timeline tests import this file under Node.
 */

const S = Math.SQRT1_2;
const DEG = Math.PI / 180;

/** Small-block proportions shared by the geometry and the piston kinematics. */
export const DIM = Object.freeze({
  bore: 0.1016,
  boreSpacing: 0.11176, // 4.40 in
  strokeRadius: 0.0442,
  rodLength: 0.1448,
  deckHeight: 0.2292, // crank axis to deck, along the bank
  pinToCrown: 0.04,
  blockHalfLength: 0.2575,
  camHeight: 0.1148,
  plugTilt: 37 * DEG, // plug axis, rotated from the outboard direction towards the bank normal
});

/**
 * The two cylinder banks, 90 degrees apart. `n` is the bank normal (the bore
 * axis), `t` points outboard across the deck, `shift` is the fore-aft offset
 * that lets two connecting rods share one crank pin.
 */
export const BANKS = Object.freeze({
  near: Object.freeze({ sign: 1, n: Object.freeze([S, S, 0]), t: Object.freeze([S, -S, 0]), shift: 0.011 }),
  far: Object.freeze({ sign: -1, n: Object.freeze([-S, S, 0]), t: Object.freeze([-S, -S, 0]), shift: -0.011 }),
});

/** Crank pin angle for each cylinder pair, front to rear (cross-plane crank), in radians. */
export const PIN_PHASE = Object.freeze([0, 90 * DEG, 270 * DEG, 180 * DEG]);

const along = (side, distance) => BANKS[side].n.map((value) => value * distance);

function plugAxis(side, distance) {
  const { n, t } = BANKS[side];
  const c = Math.cos(DIM.plugTilt) * distance;
  const s = Math.sin(DIM.plugTilt) * distance;
  return [t[0] * c + n[0] * s, t[1] * c + n[1] * s, 0];
}

/**
 * Position of each piston in the firing order 1-8-4-3-6-5-7-2, per bank, front
 * to rear. Pistons leave the block in that order.
 */
const FIRING_SLOT = Object.freeze({ near: [0, 3, 5, 6], far: [7, 2, 4, 1] });
const STAGGER = 0.012;

/**
 * @typedef {object} Part
 * @property {string} id        Unique id; the geometry builder has the same name.
 * @property {string|null} key  Legend key (`data-part`) this part answers to.
 * @property {'near'|'far'|null} bank
 * @property {number[]} explode The part's own travel when exploded (landscape), metres.
 * @property {number[]|null} window  Progress range in which the part travels.
 * @property {Part|null} rides  The part this one sits in and is carried by.
 * @property {boolean} rigid    Travel is not rescaled per layout (a piston in its bore).
 * @property {'moves'|'still'|'absent'} portrait  Behaviour in the phone layout.
 * @property {number[]|null} anchor  Where a pin points, in assembled engine space.
 * @property {number[]} [lead]       World direction for the pin's leader; default is
 *                                   away from the middle of the picture.
 * @property {number[]} [stagger]    Per-instance start delays (pistons).
 */

function part(id, explode, window, extra = {}) {
  return Object.freeze({
    id,
    key: null,
    bank: null,
    explode: Object.freeze(explode),
    window: window ? Object.freeze(window) : null,
    rides: null,
    rigid: false,
    portrait: 'moves',
    anchor: null,
    ...extra,
  });
}

/**
 * Everything that lives on one cylinder bank. Phones take only the near bank
 * apart; the far one stays assembled as a visual anchor.
 */
function bank(side) {
  const near = side === 'near';
  const mirror = (at) => (near ? at : [-at[0], at[1], at[2] - 2 * BANKS.near.shift]);
  const far = (behaviour) => (near ? 'moves' : behaviour);
  const head = part(`head_${side}`, along(side, 0.36), [0.34, 0.54], {
    bank: side,
    portrait: far('still'),
  });
  return [
    head,
    part(`cover_${side}`, along(side, 0.18), [0.26, 0.46], {
      bank: side,
      rides: head,
      portrait: far('still'),
    }),
    part(`rockers_${side}`, [0, 0, 0], [0.34, 0.54], {
      bank: side,
      rides: head,
      portrait: far('absent'),
    }),
    part(`plugs_${side}`, plugAxis(side, 0.14), [0.24, 0.42], {
      bank: side,
      rides: head,
      portrait: far('absent'),
      key: near ? 'spark_plugs' : null,
      anchor: mirror([0.322, 0.2, 0.067]),
    }),
    // Out sideways and barely down: never against the lift of the head that carries it.
    part(`header_${side}`, [BANKS[side].sign * 0.24, -0.02, 0], [0.22, 0.44], {
      bank: side,
      rides: head,
      portrait: far('absent'),
    }),
    part(`gasket_${side}`, along(side, 0.29), [0.36, 0.56], {
      bank: side,
      portrait: far('absent'),
      key: near ? 'gasket' : null,
      anchor: mirror([0.228, 0.1, 0.2]),
    }),
    part(`pistons_${side}`, along(side, 0.22), [0.42, 0.66], {
      bank: side,
      rigid: true,
      portrait: far('absent'),
      stagger: Object.freeze(FIRING_SLOT[side].map((slot) => slot * STAGGER)),
    }),
  ];
}

// The induction stack opens like an accordion: each layer lifts off the one
// below it, and the whole stack then rises with the intake manifold.
const intake = part('intake', [0, 0.13, 0], [0.28, 0.48]);
const carb = part('carb', [0, 0.12, 0], [0.16, 0.36], { rides: intake });
const airBase = part('air_base', [0, 0.085, 0], [0.14, 0.32], { rides: carb });
const airFilter = part('air_filter', [0, 0.06, 0], [0.12, 0.3], {
  rides: airBase,
  key: 'air_filter',
  anchor: [0.165, 0.462, 0.045],
  lead: [1, 0.1, -0.5], // the lid is right above: point the callout sideways
});
const airLid = part('air_lid', [0, 0.06, 0], [0.1, 0.28], { rides: airFilter });
const distributor = part('distributor', [0, 0.08, -0.1], [0.18, 0.38], { rides: intake });

/** Outer parts leave first; the block never moves. Order is also the build order. */
export const PARTS = Object.freeze([
  part('block', [0, 0, 0], null),
  ...bank('near'),
  ...bank('far'),
  intake,
  carb,
  airBase,
  airFilter,
  airLid,
  distributor,
  part('plug_wires', [0, 0, 0], [0.18, 0.38], { rides: distributor, portrait: 'absent' }),
  part('oil_pan', [0, -0.36, 0], [0.38, 0.58], { key: 'engine_oil', anchor: [0.1, -0.12, -0.02] }),
  part('oil_filter', [0.15, -0.25, 0], [0.28, 0.46], { key: 'oil_filter', anchor: [0.186, -0.06, -0.15] }),
  part('crank', [0, -0.19, 0], [0.44, 0.66]),
  part('timing_set', [0, 0, 0.1], [0.34, 0.5], { key: 'timing_kit', anchor: [0.05, 0.16, 0.272] }),
  part('timing_cover', [0, 0, 0.2], [0.3, 0.48]),
  part('water_pump', [0, 0, 0.3], [0.24, 0.44], { key: 'coolant', anchor: [0.07, 0.2, 0.33] }),
  part('pulleys', [0, 0, 0.4], [0.2, 0.38]),
  part('alternator', [-0.1, 0.04, 0.27], [0.22, 0.42]),
  part('belt', [0, 0, 0.5], [0.18, 0.36], { key: 'belt', anchor: [0.079, 0.06, 0.39] }),
  part('flywheel', [0, 0, -0.34], [0.24, 0.44], { portrait: 'absent' }),
]);

/** Parts that are built for a layout. Phones skip what they could never show well. */
export function partsFor(layout) {
  return PARTS.filter((item) => !(layout === 'portrait' && item.portrait === 'absent'));
}

/** When a legend key has no part of its own in a layout, the pin falls back to this part. */
export const KEY_FALLBACK = Object.freeze({ timing_kit: 'timing_cover', gasket: 'head_near' });

/** The part a legend key points at in a layout, or null. */
export function partForKey(key, layout) {
  const available = partsFor(layout);
  return (
    available.find((item) => item.key === key) ||
    available.find((item) => item.id === KEY_FALLBACK[key]) ||
    null
  );
}
