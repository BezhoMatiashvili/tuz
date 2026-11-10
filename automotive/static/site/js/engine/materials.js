/**
 * Material library and the studio environment for the engine.
 *
 * Eleven satin materials shared by every part, plus a small "room" that is
 * rendered through PMREM so reflections carry the brand: a white ceiling
 * softbox, a dim red wall and a yellow strip.
 */

import {
  BackSide,
  BoxGeometry,
  BufferAttribute,
  BufferGeometry,
  Color,
  CubeCamera,
  DoubleSide,
  Mesh,
  MeshBasicMaterial,
  MeshStandardMaterial,
  PlaneGeometry,
  PMREMGenerator,
  Scene,
} from '../../vendor/three@0.187.0/three.module.js';

/** name: [colour, metalness, roughness] */
export const MATERIALS = Object.freeze({
  blackSatin: ['#1B1C1E', 0, 0.55],
  castAluminium: ['#B9BDC2', 1, 0.55],
  machinedAluminium: ['#D5D8DC', 1, 0.3],
  steel: ['#8E9399', 1, 0.35],
  chrome: ['#E6E8EA', 1, 0.14],
  redGloss: ['#D2161C', 0, 0.28],
  yellow: ['#FFBC00', 0, 0.45],
  yellowPaper: ['#FFBC00', 0, 0.9],
  rubber: ['#121212', 0, 0.9],
  copper: ['#B5722F', 1, 0.4],
  zincGold: ['#C9B36B', 1, 0.45],
  ceramic: ['#E9E6DF', 0.2, 0.5],
});

const SEAT_KEY = 'engine-seat-ao';

/**
 * Build the material library.
 *
 * Vertex colours are not used as colours: red carries tint times the part's own
 * ambient occlusion, green carries the occlusion cast by neighbouring parts.
 * `seat` ({ value }) fades the neighbour term out as the engine comes apart, so
 * mating faces are dark while assembled and clean once exposed.
 */
export function createMaterials(seat) {
  const library = {};
  const patch = (shader) => {
    shader.uniforms.uSeat = seat;
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', '#include <common>\nuniform float uSeat;')
      .replace(
        '#include <color_fragment>',
        'diffuseColor.rgb *= vColor.r * mix( 1.0, vColor.g, uSeat );',
      );
  };
  for (const [name, [colour, metalness, roughness]] of Object.entries(MATERIALS)) {
    const material = new MeshStandardMaterial({
      color: colour,
      metalness,
      roughness,
      vertexColors: true,
    });
    material.name = name;
    material.onBeforeCompile = patch;
    material.customProgramCacheKey = () => SEAT_KEY;
    library[name] = material;
  }
  return library;
}

/** A soft blob of darkness for under the engine: vertex alpha, no texture needed. */
export function createContactShadow(radius = 1, rings = 12, segments = 48) {
  const positions = [0, 0, 0];
  const colours = [0, 0, 0, 1];
  for (let ring = 1; ring <= rings; ring += 1) {
    const t = ring / rings;
    const falloff = (1 - t * t * (3 - 2 * t)) ** 1.6;
    for (let s = 0; s < segments; s += 1) {
      const angle = (s / segments) * Math.PI * 2;
      positions.push(Math.cos(angle) * radius * t, 0, Math.sin(angle) * radius * t);
      colours.push(0, 0, 0, falloff);
    }
  }
  const index = [];
  for (let s = 0; s < segments; s += 1) {
    index.push(0, 1 + ((s + 1) % segments), 1 + s);
  }
  for (let ring = 1; ring < rings; ring += 1) {
    const inner = 1 + (ring - 1) * segments;
    const outer = inner + segments;
    for (let s = 0; s < segments; s += 1) {
      const next = (s + 1) % segments;
      index.push(inner + s, inner + next, outer + s, inner + next, outer + next, outer + s);
    }
  }
  const geometry = new BufferGeometry();
  geometry.setAttribute('position', new BufferAttribute(new Float32Array(positions), 3));
  geometry.setAttribute('color', new BufferAttribute(new Float32Array(colours), 4));
  geometry.setIndex(index);
  const material = new MeshBasicMaterial({
    vertexColors: true,
    transparent: true,
    depthWrite: false,
    toneMapped: false,
  });
  const mesh = new Mesh(geometry, material);
  mesh.renderOrder = -1;
  return mesh;
}

function emitter(width, height, colour, strength, position, lookAt) {
  const material = new MeshBasicMaterial({ side: DoubleSide, toneMapped: false });
  material.color.set(colour).multiplyScalar(strength);
  const mesh = new Mesh(new PlaneGeometry(width, height), material);
  mesh.position.set(position[0], position[1], position[2]);
  mesh.lookAt(lookAt[0], lookAt[1], lookAt[2]);
  return mesh;
}

/** The lighting room. The camera sits front-right of the engine, at about +X +Z. */
function createRoom() {
  const room = new Scene();
  // The shell closes the room, so this never shows; it only has to be defined.
  room.background = new Color(0x000000);
  const shell = new Mesh(
    new BoxGeometry(16, 10, 16),
    new MeshBasicMaterial({ side: BackSide, toneMapped: false }),
  );
  shell.material.color.setRGB(0.16, 0.155, 0.15);
  shell.position.set(0, 2.2, 0);
  room.add(shell);

  // A pale floor: the bounce that keeps undersides from going black.
  const floor = new Mesh(
    new PlaneGeometry(16, 16),
    new MeshBasicMaterial({ toneMapped: false }),
  );
  floor.material.color.setRGB(0.5, 0.48, 0.46);
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -1.4;
  room.add(floor);

  room.add(
    // Ceiling softbox: the broad white that gives satin metal its body.
    emitter(6, 6, '#FFFFFF', 7, [0.5, 4.8, 0.8], [0, 0, 0]),
    // Backdrop behind the engine, opposite the camera. At the low camera angles of
    // this section it is what every upward face reflects. Two bands with a dark
    // gap between them, so chrome has something with contrast to mirror.
    emitter(11, 2, '#FFFFFF', 3.4, [-5.2, 4.4, -5], [0, 1.6, 0]),
    emitter(11, 1.5, '#FFFFFF', 1.7, [-5.2, 2, -5], [0, 0.6, 0]),
    emitter(11, 1.7, '#D2161C', 1.1, [-5.2, 0.2, -5], [0, 0.2, 0]),
    // Large dim fill behind the camera so faces towards the viewer never go black.
    emitter(7, 4, '#FFF6EA', 1.1, [5, 1.6, 5.5], [0, 0.2, 0]),
    // Tall key strip, front-left: the crisp vertical highlight on chrome and paint.
    emitter(1.1, 4.2, '#FFFFFF', 9, [-4.2, 2.2, 4.6], [0, 0.4, 0]),
    // The shop's red wall, low on the viewer's left.
    emitter(7, 2.6, '#D2161C', 1.5, [-6.5, -0.1, 1.5], [0, 0.3, 0]),
    // Yellow stripe, low on the right: a warm line in the glossy edges.
    emitter(6, 0.36, '#FFBC00', 3.2, [6, 0.1, -2.2], [0, 0.2, 0]),
  );
  return room;
}

function disposeRoom(room) {
  room.traverse((object) => {
    if (object.isMesh) {
      object.geometry.dispose();
      object.material.dispose();
    }
  });
}

const ENV = { blur: 0.035, near: 0.1, far: 40, size: 256 };

/** The pieces of three's PMREMGenerator.fromScene, which `planEnvironment` runs one at a time. */
const STAGES = [
  '_setSize',
  '_allocateTarget',
  '_getSourceTarget',
  '_blur',
  '_applyPMREM',
  '_renderCube',
  '_compileMaterial',
];

/**
 * The prefiltered environment map, as a plan: `fromScene` taken apart into the
 * passes it is made of, so the caller can hand the GPU one pass at a time and
 * never wait for it.
 *
 * - `target` is the render target; `target.texture` can be the scene's
 *   environment straight away, it is filled by the jobs.
 * - `jobs` is the work in order, each entry one small thing to send to the GPU:
 *   first the shader programs the passes use (the room's, then one filter at a
 *   time), so they have linked long before they are needed, then the passes
 *   themselves: the capture of the room, two blur passes and one filter pass
 *   per mip level, six small faces each.
 * - `release()` frees what only the passes needed.
 *
 * The passes are the generator's own (private) methods, replayed in the order
 * fromScene calls them; three.js is vendored at one exact version. Should that
 * ever not match, the whole map is rendered in one go instead and `jobs` is empty.
 *
 * Call again after a restored WebGL context: render targets do not survive a
 * context loss.
 */
export function planEnvironment(renderer) {
  const room = createRoom();
  const generator = new PMREMGenerator(renderer);
  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    generator.dispose();
    disposeRoom(room);
  };
  if (!STAGES.every((name) => typeof generator[name] === 'function')) {
    const target = generator.fromScene(room, ENV.blur, ENV.near, ENV.far);
    release();
    return { target, jobs: [], release };
  }

  generator._setSize(ENV.size);
  const target = generator._allocateTarget();
  const source = generator._getSourceTarget(true);
  const camera = new CubeCamera(ENV.near, ENV.far, source);

  // Let the generator set up its two filters, but note each pass instead of drawing it.
  const passes = [];
  generator._renderCube = (to, lod, material) => {
    const values = {};
    for (const [name, uniform] of Object.entries(material.uniforms)) values[name] = uniform.value;
    passes.push({ to, lod, material, values });
  };
  try {
    generator._blur(target, ENV.blur);
    generator._applyPMREM(target);
  } finally {
    delete generator._renderCube;
  }

  // Programs depend on what they draw into: compile with a target bound, as the passes run.
  const compiling = (compile) => () => {
    renderer.setRenderTarget(source, 0);
    compile();
    renderer.setRenderTarget(null);
  };
  const jobs = [
    compiling(() => renderer.compile(room, camera.children[0])),
    ...[...new Set(passes.map((pass) => pass.material))].map((material) =>
      compiling(() => generator._compileMaterial(material)),
    ),
    () => {
      // As fromScene does it: the source mips are made once the blur has been through.
      source.texture.mipmapsAutoUpdate = false;
      camera.update(renderer, room);
      source.texture.mipmapsAutoUpdate = true;
    },
    ...passes.map((pass) => () => {
      for (const [name, value] of Object.entries(pass.values)) {
        pass.material.uniforms[name].value = value;
      }
      generator._renderCube(pass.to, pass.lod, pass.material);
    }),
  ];

  return { target, jobs, release };
}

/** The environment map in one go, for the rare rebuild after a context loss. */
export function createEnvironment(renderer) {
  const plan = planEnvironment(renderer);
  for (const job of plan.jobs) job();
  plan.release();
  return plan.target;
}

export function disposeMaterials(library) {
  for (const material of Object.values(library)) material.dispose();
}
