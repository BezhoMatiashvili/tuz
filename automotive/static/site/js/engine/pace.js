/**
 * Cooperative scheduling for the 3D start-up.
 *
 * Building the engine is a few hundred milliseconds of work on a phone. None
 * of it is urgent, so it is done a few milliseconds at a time with the thread
 * handed back in between: input and frames always come first, and no single
 * task of the build is long.
 *
 * No DOM and no three.js in here.
 */

/** How long one slice of start-up work runs before the thread is handed back. */
export const SLICE_MS = 6;
/** How much of it is done per animation frame before the next frame is waited for. */
export const FRAME_WORK_MS = 10;
/** How long to wait for a frame that may never come (a hidden tab draws none). */
const FRAME_WAIT_MS = 100;

let port = null;
const waiting = [];

function post(resume) {
  if (!port && typeof MessageChannel === 'function') {
    const channel = new MessageChannel();
    channel.port1.onmessage = () => {
      const next = waiting.shift();
      if (next) next();
    };
    port = channel.port2;
  }
  if (!port) {
    setTimeout(resume, 0);
    return;
  }
  waiting.push(resume);
  port.postMessage(0);
}

/**
 * A promise for a fresh task: `scheduler.yield()` where the browser has it,
 * otherwise a message, otherwise a timer.
 *
 * `scheduler.yield()` continues at the priority of the task it is called in.
 * Called from ordinary code that is ahead of rendering, and a chain of yields
 * then holds frames back to about ten a second. So the start-up is run inside
 * `inBackground`, where every yield inherits background priority and frames,
 * input and the page's own tasks all go first.
 */
export function nextTask() {
  const scheduler = globalThis.scheduler;
  if (scheduler && typeof scheduler.yield === 'function') return scheduler.yield();
  return new Promise(post);
}

/**
 * Run `work` (which may be async) as a background-priority task and resolve
 * with its result. Where the scheduler API is missing it is simply called in
 * a fresh task; the message and timer yields are fair to rendering anyway.
 */
export function inBackground(work) {
  const scheduler = globalThis.scheduler;
  if (scheduler && typeof scheduler.postTask === 'function') {
    return scheduler.postTask(work, { priority: 'background' });
  }
  return new Promise(post).then(work);
}

/**
 * A promise for the task after the next animation frame, so the browser has
 * drawn in between. Resolves with the time the frame began, or with 0 when no
 * frame came (a hidden tab) and the wait timed out.
 */
function afterFrame() {
  return new Promise((resolve) => {
    let timer = 0;
    const frame = requestAnimationFrame(() => {
      clearTimeout(timer);
      const at = performance.now();
      post(() => resolve(at));
    });
    timer = setTimeout(() => {
      cancelAnimationFrame(frame);
      resolve(0);
    }, FRAME_WAIT_MS);
  });
}

function drawing() {
  return (
    typeof requestAnimationFrame === 'function' && typeof document !== 'undefined' && !document.hidden
  );
}

/** A promise that resolves after `ms` milliseconds, in a task of its own. */
export function delay(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function aborted() {
  return new DOMException('engine build aborted', 'AbortError');
}

/**
 * A pacer times the current slice of work and says when to stop for a moment.
 * Every pause is also where an aborted build stops for good: each method that
 * waits throws an AbortError once `signal` is aborted.
 *
 * It also keeps frames on time. While a visitor scrolls by touch the browser
 * ranks its own main-thread frames below almost everything, so a chain of
 * yields alone can hold them back to about ten a second. After FRAME_WORK_MS
 * of work since the last frame the pacer therefore waits for the next one.
 *
 * `report` describes the work so far: how many slices, how long they were
 * busy in total, the longest one with the stage it belonged to, the longest
 * single step of a generator (the smallest unit that cannot be cut), and the
 * longest slice of each kind of stage.
 */
export function createPacer(signal, budget = SLICE_MS) {
  const report = { slices: 0, busy: 0, longest: 0, at: '', step: 0, stepAt: '', stages: {} };
  let stage = '';
  let began = performance.now();
  let frameAt = began;

  /** A fresh task; after the next frame if this one has had its share of work. */
  async function pause() {
    if (drawing() && performance.now() - frameAt >= FRAME_WORK_MS) {
      frameAt = (await afterFrame()) || performance.now();
    } else {
      await nextTask();
    }
  }

  function close() {
    const spent = performance.now() - began;
    report.slices += 1;
    report.busy += spent;
    if (spent > report.longest) {
      report.longest = spent;
      report.at = stage;
    }
    // "part:block" and "part:crank" are the same kind of work: keep one figure for both.
    const kind = stage.startsWith('part:') ? 'part' : stage;
    if (!(spent <= report.stages[kind])) report.stages[kind] = spent;
  }

  function open() {
    began = performance.now();
    if (signal && signal.aborted) throw aborted();
  }

  const pacer = {
    report,

    /** Name the work that follows, for the report and the performance timeline. */
    mark(name) {
      stage = name;
      performance.mark(`engine:${name}`);
    },

    /** The name given last. */
    stage() {
      return stage;
    },

    /** True once the current slice has used its budget. */
    due() {
      return performance.now() - began >= budget;
    },

    /** Hand the thread back now and carry on in a fresh task. */
    async rest() {
      close();
      await pause();
      open();
    },

    /** Hand the thread back only if the slice is used up. */
    async breathe() {
      if (pacer.due()) await pacer.rest();
    },

    /**
     * Wait for something that is not this build's own work (a module, the GPU)
     * and carry on in a fresh task, so the wait is never counted as a slice
     * and whatever resolved the promise keeps its task to itself.
     */
    async wait(promise) {
      close();
      let value;
      try {
        value = await promise;
      } finally {
        await pause();
      }
      open();
      return value;
    },

    /** Run a generator to its end, one slice at a time. Returns its return value. */
    async run(steps) {
      for (;;) {
        let from = performance.now();
        if (from - began >= budget) {
          await pacer.rest();
          from = performance.now();
        }
        const step = steps.next();
        const took = performance.now() - from;
        if (took > report.step) {
          report.step = took;
          report.stepAt = step.value ? `${stage}:${step.value}` : stage;
        }
        if (step.done) return step.value;
      }
    },
  };
  return pacer;
}
