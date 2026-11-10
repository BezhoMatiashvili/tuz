/**
 * The live 3D tier: renderer, lights, the assembled model and `render(p)`.
 *
 * Nothing here animates on its own. `render(p)` poses every part for scroll
 * progress p and draws one frame; the caller decides when p has changed.
 *
 * Start-up is the expensive part, and it is cooperative. The model is built a
 * few milliseconds at a time (pace.js). The GPU gets one small job at a time
 * and is asked, without waiting, whether it has finished before the next one
 * is sent: nothing in here blocks on it, and the canvas is not shown until its
 * first frame is done.
 */

import {
  DirectionalLight,
  Group,
  InstancedMesh,
  Mesh,
  NeutralToneMapping,
  PerspectiveCamera,
  Scene,
  Vector3,
  WebGLRenderer,
} from '../../vendor/three@0.187.0/three.module.js';
import { buildPart, prime } from './geometry.js';
import {
  createContactShadow,
  createEnvironment,
  createMaterials,
  disposeMaterials,
  planEnvironment,
} from './materials.js';
import { createPacer, delay } from './pace.js';
import { partForKey, partsFor } from './parts.js';
import {
  buildCameraTrack,
  cameraAt,
  CAMERA,
  cameraTrackSteps,
  crankAngle,
  partOffset,
  seatAmount,
  smoothstep,
} from './timeline.js';

/** Half-extents of the contact shadow, and how much of it the camera must keep in frame. */
const SHADOW = { x: 0.5, z: 0.4, y: -0.2, keep: 0.5 };
/** How often the GPU is asked whether it has finished, and when to stop asking. */
const POLL_MS = 4;
const STALLED_MS = 5000;

/**
 * The GPU as a lane with one job in it at a time.
 *
 * `touch()` after sending commands drops a fence behind them. `free()` says,
 * without blocking, whether the GPU has passed that fence and every shader
 * program has linked. Anything that would make the browser wait for the GPU
 * (first use of a program, showing a frame) is only done on a free lane, so
 * the wait is always short.
 */
function createLane(gl, renderer) {
  const parallel = renderer.extensions.has('KHR_parallel_shader_compile');
  let fence = null;
  let since = 0;

  function passed() {
    if (!fence) return true;
    if (!gl.isContextLost()) {
      const status = gl.clientWaitSync(fence, 0, 0);
      if (status === gl.TIMEOUT_EXPIRED && performance.now() - since < STALLED_MS) return false;
      gl.deleteSync(fence);
    }
    fence = null;
    return true;
  }

  function linked() {
    if (!parallel || gl.isContextLost()) return true;
    const programs = renderer.info.programs;
    for (let i = 0; i < programs.length; i += 1) {
      if (!programs[i].isReady()) return false;
    }
    return true;
  }

  const lane = {
    touch() {
      if (gl.isContextLost()) return;
      if (fence) gl.deleteSync(fence);
      fence = gl.fenceSync(gl.SYNC_GPU_COMMANDS_COMPLETE, 0);
      since = performance.now();
      gl.flush();
    },
    free() {
      return passed() && linked();
    },
    /** Resolves once the lane is free; the value is how long that took, in milliseconds. */
    async clear() {
      const from = performance.now();
      // A fence is never reported as passed in the task that set it: ask in later ones.
      while (!lane.free()) await delay(POLL_MS);
      return performance.now() - from;
    },
  };
  return lane;
}

/**
 * Build the engine on `canvas`, without ever holding the thread for long.
 *
 * @param {object} options
 * @param {HTMLCanvasElement} options.canvas
 * @param {'portrait'|'landscape'} options.layout  Also the detail tier.
 * @param {number} options.width   CSS pixels of the art box.
 * @param {number} options.height
 * @param {number} options.pixelRatio
 * @param {number[]} [options.safe]  [left, top, right, bottom] of the frame to compose in.
 * @param {WebGL2RenderingContext} [options.context]  An existing context of `canvas`.
 * @param {AbortSignal} [options.signal]
 * @param {object} [options.pacer]  A pacer already in use (pace.js); one is made otherwise.
 * @param {boolean} [options.debug]  Report shader errors (costs a wait per program).
 * @param {() => void} [options.onContextLost]
 * @param {() => void} [options.onContextRestored]
 * @throws when WebGL2 is unavailable or the build is aborted.
 */
export async function createEngineScene(options) {
  const { canvas, layout, signal } = options;
  const began = performance.now();
  const pacer = options.pacer || createPacer(signal);

  // The renderer is one piece of three.js that cannot be cut: give it a task to itself.
  await pacer.rest();
  pacer.mark('renderer');
  const renderer = new WebGLRenderer({
    canvas,
    context: options.context,
    antialias: true,
    alpha: true,
    stencil: false,
    powerPreference: 'high-performance',
  });
  renderer.debug.checkShaderErrors = options.debug === true;
  renderer.toneMapping = NeutralToneMapping;
  renderer.toneMappingExposure = 1;
  renderer.setClearColor(0x000000, 0);

  let width = Math.max(1, options.width);
  let height = Math.max(1, options.height);
  let pixelRatio = options.pixelRatio;
  // One call, so the drawing buffer is allocated once (and not at all when the
  // canvas already has this size).
  renderer.setDrawingBufferSize(width, height, pixelRatio);
  const gl = renderer.getContext();
  const lane = createLane(gl, renderer);

  const scene = new Scene();
  const camera = new PerspectiveCamera(CAMERA.fov, width / height, 0.5, 30);
  const seat = { value: 1 };
  const materials = createMaterials(seat);

  const key = new DirectionalLight(0xffffff, 3);
  key.position.set(-2.5, 4, 3);
  const rimWarm = new DirectionalLight('#FFDDA0', 1.9);
  rimWarm.position.set(3, 2.5, -3);
  const rimRed = new DirectionalLight('#FF5A4D', 1.4);
  rimRed.position.set(-3, 1, -2.5);
  scene.add(key, rimWarm, rimRed);

  const shadow = createContactShadow(1);
  shadow.scale.set(SHADOW.x, 1, SHADOW.z);
  shadow.visible = false;
  scene.add(shadow);

  /** @type {Array<object>} */
  const entries = [];
  let environment = null;
  let plan = null;
  let disposed = false;

  function dispose() {
    if (disposed) return;
    disposed = true;
    canvas.removeEventListener('webglcontextlost', onLost);
    canvas.removeEventListener('webglcontextrestored', onRestored);
    for (const entry of entries) {
      entry.group.traverse((object) => {
        if (object.isInstancedMesh) object.dispose();
        if (object.isMesh) object.geometry.dispose();
      });
    }
    shadow.geometry.dispose();
    shadow.material.dispose();
    disposeMaterials(materials);
    if (plan) plan.release();
    if (environment) environment.dispose();
    // Leave the shared context in its default state for whoever builds on it next.
    renderer.resetState();
    renderer.dispose();
  }

  function onLost(event) {
    event.preventDefault();
    if (options.onContextLost) options.onContextLost();
  }

  function onRestored() {
    if (disposed || !ready) return;
    // Render targets do not survive a context loss: rebuild the environment. The
    // old one died with the old context, so there is nothing left to dispose.
    environment = createEnvironment(renderer);
    scene.environment = environment.texture;
    render();
    if (options.onContextRestored) options.onContextRestored();
  }

  canvas.addEventListener('webglcontextlost', onLost);
  canvas.addEventListener('webglcontextrestored', onRestored);

  let safe = options.safe;
  let track = null;
  let ready = false;
  let current = 0;
  const view = {};
  const state = { p: 0, crank: 0, layout };
  const offset = [0, 0, 0];
  let pan = null;
  let posed = null;
  let supportCount = 0;
  const SHADOW_POINTS = 8;

  function samplePoints(p) {
    let write = 0;
    for (const entry of entries) {
      partOffset(entry.part, p, layout, offset);
      const support = entry.support;
      for (let i = 0; i < support.length; i += 3) {
        posed[write] = support[i] + offset[0];
        posed[write + 1] = support[i + 1] + offset[1];
        posed[write + 2] = support[i + 2] + offset[2];
        write += 3;
      }
    }
    // The dense middle of the contact shadow has to stay in frame too.
    if (pan) partOffset(pan.part, p, layout, offset);
    for (let i = 0; i < SHADOW_POINTS; i += 1) {
      const angle = (i / SHADOW_POINTS) * Math.PI * 2;
      posed[write] = Math.cos(angle) * SHADOW.x * SHADOW.keep;
      posed[write + 1] = SHADOW.y + (pan ? offset[1] : 0);
      posed[write + 2] = Math.sin(angle) * SHADOW.z * SHADOW.keep;
      write += 3;
    }
    return { points: posed, count: supportCount };
  }

  function pose(p) {
    current = p;
    state.p = p;
    state.crank = crankAngle(p);
    for (const entry of entries) {
      if (!entry.selfPosed) {
        partOffset(entry.part, p, layout, offset);
        entry.group.position.set(offset[0], offset[1], offset[2]);
      }
      for (const spinner of entry.spinners) spinner.pivot.rotation.z = state.crank * spinner.ratio;
      for (const item of entry.instanced) item.update(item.mesh, state);
    }
    seat.value = seatAmount(p);

    // The contact shadow stays under whatever is lowest: it follows the oil pan down.
    shadow.position.set(0, SHADOW.y + (pan ? pan.group.position.y : 0), 0);
    shadow.material.opacity = 0.62 - 0.2 * smoothstep(0.1, 0.68, p);

    cameraAt(track, p, view);
    camera.position.set(view.position[0], view.position[1], view.position[2]);
    camera.lookAt(view.target[0], view.target[1], view.target[2]);
    // Off-axis frustum: the camera looks through the centre of the safe area.
    const near = Math.max(0.2, view.distance - 3);
    const frame = track.window;
    camera.projectionMatrix.makePerspective(
      frame.left * near,
      frame.right * near,
      frame.top * near,
      frame.bottom * near,
      near,
      view.distance + 4,
    );
    camera.projectionMatrixInverse.copy(camera.projectionMatrix).invert();
  }

  function render(p = current) {
    if (disposed || !ready || gl.isContextLost()) return;
    pose(p);
    renderer.render(scene, camera);
  }

  function resize(nextWidth, nextHeight, nextRatio = pixelRatio, nextSafe = safe) {
    const w = Math.max(1, Math.round(nextWidth));
    const h = Math.max(1, Math.round(nextHeight));
    const safeChanged = String(nextSafe) !== String(safe);
    if (w === width && h === height && nextRatio === pixelRatio && !safeChanged) return;
    const aspectChanged = Math.abs(w / h - width / height) > 1e-4;
    width = w;
    height = h;
    pixelRatio = nextRatio;
    safe = nextSafe;
    renderer.setDrawingBufferSize(width, height, pixelRatio);
    if (aspectChanged || safeChanged) {
      track = buildCameraTrack(samplePoints, width / height, { safe });
    }
    render();
  }

  // ---- start-up -----------------------------------------------------------

  /** Programs to create and environment passes to draw: GPU work still to be sent. */
  let jobs = [];

  /** Send the next job if the GPU has finished the last one; otherwise carry on. */
  async function pump() {
    if (!jobs.length || !lane.free()) return;
    const stage = pacer.stage();
    await pacer.rest();
    pacer.mark('gpu:job');
    jobs.shift()();
    lane.touch();
    await pacer.rest();
    pacer.mark(stage);
  }

  try {
    await pacer.rest();

    pacer.mark('environment');
    plan = planEnvironment(renderer);
    environment = plan.target;
    scene.environment = environment.texture;
    scene.environmentIntensity = 0.9;
    // One at a time, in between the parts below: the GPU process draws the page
    // too, and a burst of shader compiles would hold its frames up.
    jobs = [...plan.jobs, () => renderer.compile(shadow, camera, scene)];
    await pump();

    pacer.mark('prime');
    await pacer.rest();
    await pacer.run(prime());
    await pump();

    for (const part of partsFor(layout)) {
      pacer.mark(`part:${part.id}`);
      const built = await pacer.run(buildPart(part, layout));
      const group = new Group();
      group.name = part.id;
      for (const item of built.meshes) group.add(new Mesh(item.geometry, materials[item.material]));
      const spinners = built.spinners.map((spinner) => {
        const pivot = new Group();
        pivot.position.set(spinner.center[0], spinner.center[1], spinner.center[2]);
        for (const item of spinner.meshes) pivot.add(new Mesh(item.geometry, materials[item.material]));
        group.add(pivot);
        return { pivot, ratio: spinner.ratio };
      });
      const instanced = built.instanced.map((item) => {
        const mesh = new InstancedMesh(item.geometry, materials[item.material], item.count);
        mesh.frustumCulled = false;
        group.add(mesh);
        return { mesh, update: item.update };
      });
      // Hidden until it has been drawn once on its own (see the warm-up below).
      group.visible = false;
      scene.add(group);
      entries.push({ part, group, spinners, instanced, selfPosed: built.selfPosed, support: built.support });
      // Creates the part's programs if they are new; they link while the rest is built.
      await pacer.breathe();
      const known = renderer.info.programs.length;
      renderer.compile(group, camera, scene);
      if (renderer.info.programs.length !== known) {
        lane.touch();
        await pacer.rest();
      }
      await pump();
    }

    pacer.mark('camera');
    pan = entries.find((entry) => entry.part.id === 'oil_pan') || null;
    supportCount = entries.reduce((sum, entry) => sum + entry.support.length / 3, 0) + SHADOW_POINTS;
    posed = new Float32Array(supportCount * 3);
    await pacer.breathe();
    track = await pacer.run(cameraTrackSteps(samplePoints, width / height, { safe }));

    pacer.mark('gpu:rest');
    while (jobs.length) {
      await pacer.wait(lane.clear());
      jobs.shift()();
      lane.touch();
    }
    await pacer.wait(lane.clear());
    plan.release();

    // Draw every part once on its own into one corner pixel. That uploads its
    // buffers and uses its programs for the first time, a part or two per task,
    // so the first real frame has nothing left to set up.
    pacer.mark('warm-up');
    pose(0);
    ready = true;
    renderer.setViewport(0, 0, 1, 1);
    renderer.setScissor(0, 0, 1, 1);
    renderer.setScissorTest(true);
    const solo = [shadow, ...entries.map((entry) => entry.group)];
    let index = 0;
    while (index < solo.length) {
      await pacer.wait(lane.clear());
      do {
        solo[index].visible = true;
        renderer.render(scene, camera);
        solo[index].visible = false;
        index += 1;
      } while (index < solo.length && !pacer.due());
      lane.touch();
    }
    await pacer.wait(lane.clear());
    for (const object of solo) object.visible = true;
    renderer.setScissorTest(false);
    renderer.setViewport(0, 0, width, height);
    renderer.setScissor(0, 0, width, height);
    pacer.mark('ready');
  } catch (error) {
    dispose();
    throw error;
  }

  const boot = {
    ms: performance.now() - began,
    busy: pacer.report.busy,
    slices: pacer.report.slices,
    longest: pacer.report.longest,
    at: pacer.report.at,
    step: pacer.report.step,
    stepAt: pacer.report.stepAt,
    stages: { ...pacer.report.stages },
  };

  const anchor = new Vector3();
  const ahead = new Vector3();

  /**
   * Where a legend key's part is on screen, as fractions of the art box: out[0]
   * and out[1]. If the part asks for a leader direction, out[2] and out[3] are
   * that direction on screen (else 0, 0). Call after `render`. Returns false
   * when the part is missing or off frame.
   */
  function project(keyName, out) {
    const part = partForKey(keyName, layout);
    const entry = part && entries.find((item) => item.part === part);
    if (!entry) return false;
    const at = part.anchor;
    if (at) {
      anchor.set(at[0], at[1], at[2]);
    } else {
      anchor.set(0, 0, 0);
      const support = entry.support;
      for (let i = 0; i < support.length; i += 3) {
        anchor.x += support[i];
        anchor.y += support[i + 1];
        anchor.z += support[i + 2];
      }
      anchor.multiplyScalar(3 / support.length);
    }
    anchor.add(entry.group.position);
    out[2] = 0;
    out[3] = 0;
    if (part.lead) {
      ahead.set(part.lead[0], part.lead[1], part.lead[2]).multiplyScalar(0.05).add(anchor).project(camera);
    }
    anchor.project(camera);
    if (part.lead) {
      out[2] = (ahead.x - anchor.x) * width;
      out[3] = -(ahead.y - anchor.y) * height;
    }
    out[0] = (anchor.x + 1) / 2;
    out[1] = (1 - anchor.y) / 2;
    return anchor.z < 1 && out[0] > 0.02 && out[0] < 0.98 && out[1] > 0.02 && out[1] < 0.98;
  }

  /**
   * What one full frame costs the GPU, in milliseconds: the best of a few,
   * timed from the draw calls to the fence behind them. Nothing is shown.
   */
  async function probe(frames = 3) {
    let best = Infinity;
    for (let i = 0; i < frames; i += 1) {
      await lane.clear();
      if (disposed || gl.isContextLost()) return Infinity;
      render();
      lane.touch();
      best = Math.min(best, await lane.clear());
    }
    return best;
  }

  /** Resolves when the GPU has finished everything drawn so far. */
  async function settled() {
    lane.touch();
    await lane.clear();
  }

  return {
    layout,
    render,
    resize,
    project,
    probe,
    settled,
    dispose,
    /**
     * How the start-up went: total and busy milliseconds, the number of slices,
     * the longest slice and its stage, the longest single step, and the longest
     * slice of each kind of stage.
     */
    boot,
    get pixelRatio() {
      return pixelRatio;
    },
    /** Draw calls and triangles of the last rendered frame. */
    stats() {
      return {
        calls: renderer.info.render.calls,
        triangles: renderer.info.render.triangles,
        programs: renderer.info.programs.length,
      };
    },
  };
}
