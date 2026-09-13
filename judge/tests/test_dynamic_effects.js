const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../resources/dynamic-effects.js'), 'utf8');

async function scenario({width = 1200, reduced = false, stored = null, blocked = false, effect = 'snow', fail = false, missingLibrary = false, deferred = false} = {}) {
  const events = {}, nodes = {}, loads = [], scripts = [], finishLoads = [];
  let destroyed = 0;
  function element(id) {
    const node = {id, style: {setProperty() {}}, children: [], className: '', checked: false,
      getAttribute: key => key.replace('data-', ''),
      addEventListener(name, fn) { this[name] = fn; },
      appendChild(child) { this.children.push(child); if (child.id) nodes[child.id] = child; },
      querySelector() { return input; }, remove() { delete nodes[this.id]; }};
    if (id) nodes[id] = node;
    return node;
  }
  const status = element('dynamic-effect-status');
  const input = element();
  element('dynamic-effect-motion-override');
  element('dynamic-effect-retry');
  element('dynamic-effect-retry-wrapper');
  const motion = {matches: reduced, addEventListener(name, fn) { this.change = fn; }};
  const particles = {load: async (id, options) => {
    if (fail) throw Error('Initialization failed');
    loads.push({id, options});
    if (deferred) await new Promise(resolve => finishLoads.push(resolve));
    return {destroy() { destroyed++; }};
  }};
  const context = {
    window: {innerWidth: width, innerHeight: 900, DYNAMIC_EFFECT: effect,
      DYNAMIC_EFFECT_LIBRARY: '/static/tsparticles-2.12.0.bundle.min.js',
      matchMedia: () => motion, addEventListener: (name, fn) => { events[name] = fn; }},
    navigator: {hardwareConcurrency: 2, deviceMemory: 2},
    localStorage: {getItem() { if (blocked) throw Error('Storage blocked'); return stored; },
      setItem(key, value) { if (blocked) throw Error('Storage blocked'); stored = value; }},
    document: {readyState: 'complete', getElementById: id => nodes[id],
      querySelector: () => input, createElement: () => element(), body: element(),
      head: {appendChild(script) { scripts.push(script); }}},
    tsParticles: particles, console: {warn() {}}, setTimeout, clearTimeout
  };
  if (missingLibrary) delete context.tsParticles;
  vm.runInNewContext(source, context);
  async function settle() { await new Promise(resolve => setImmediate(resolve)); }
  await settle();
  return {context, motion, nodes, loads, scripts, input, status, events, settle, finishLoads, particles,
    destroyed: () => destroyed, stored: () => stored};
}

(async () => {
  const small = await scenario({width: 375});
  assert.equal(small.status.textContent, 'running');
  assert.ok(small.loads[0].options.particles.number.value < 28);
  assert.equal(small.loads[0].options.particles.number.density.enable, false);
  const paused = await scenario({reduced: true});
  assert.equal(paused.status.textContent, 'paused');
  assert.equal(paused.loads.length, 0);
  paused.input.checked = true;
  paused.input.change();
  await paused.settle();
  assert.equal(paused.status.textContent, 'running');
  assert.equal(paused.stored(), 'true');
  paused.input.checked = false;
  paused.input.change();
  await paused.settle();
  assert.equal(paused.status.textContent, 'paused');
  assert.equal(paused.destroyed(), 1);
  assert.equal((await scenario({reduced: true, stored: 'true'})).status.textContent, 'running');
  const blocked = await scenario({reduced: true, blocked: true});
  blocked.input.checked = true;
  blocked.input.change();
  await blocked.settle();
  assert.equal(blocked.status.textContent, 'running');
  small.context.window.innerWidth = 1400;
  small.events.resize();
  await new Promise(resolve => setTimeout(resolve, 350));
  assert.equal(small.destroyed(), 1);
  assert.equal(small.loads.length, 2);
  small.motion.matches = true;
  small.motion.change();
  await small.settle();
  assert.equal(small.status.textContent, 'paused');
  small.events.storage({key: 'dynamic-effects-motion-override', newValue: 'true'});
  await small.settle();
  assert.equal(small.status.textContent, 'running');
  small.events.storage({key: null});
  await small.settle();
  assert.equal(small.status.textContent, 'paused');
  const lunar = await scenario({effect: 'lunar_new_year'});
  assert.equal(lunar.loads.length, 3);
  lunar.motion.matches = true;
  lunar.motion.change();
  await lunar.settle();
  assert.equal(lunar.destroyed(), 3);
  assert.equal((await scenario({effect: 'none'})).status.textContent, 'none');
  assert.equal((await scenario({effect: 'red_poinciana', width: 375})).status.textContent, 'running');
  assert.equal((await scenario({fail: true})).status.textContent, 'failed');
  const library = await scenario({effect: 'none'});
  delete library.context.tsParticles;
  library.context.window.DYNAMIC_EFFECT = 'snow';
  library.nodes['dynamic-effect-retry'].click();
  await library.settle();
  assert.equal(library.status.textContent, 'loading');
  assert.equal(library.scripts[0].src, library.context.window.DYNAMIC_EFFECT_LIBRARY);
  library.scripts[0].onerror();
  await library.settle();
  assert.equal(library.status.textContent, 'failed');
  assert.equal(library.nodes['dynamic-effect-retry-wrapper'].hidden, false);
  library.nodes['dynamic-effect-retry'].click();
  await library.settle();
  library.context.tsParticles = {load: async () => ({destroy() {}})};
  library.scripts[1].onload();
  await library.settle();
  assert.equal(library.status.textContent, 'running');
  assert.equal(library.nodes['dynamic-effect-retry-wrapper'].hidden, true);

  // A motion preference change must win over a library request already in flight.
  const slowLibrary = await scenario({missingLibrary: true});
  slowLibrary.motion.matches = true;
  slowLibrary.motion.change();
  assert.equal(slowLibrary.status.textContent, 'paused');
  slowLibrary.context.tsParticles = slowLibrary.particles;
  slowLibrary.scripts[0].onload();
  await slowLibrary.settle();
  assert.equal(slowLibrary.loads.length, 0);
  assert.equal(slowLibrary.status.textContent, 'paused');

  // Stale particle instances must be destroyed when asynchronous initialization finishes.
  const slowParticles = await scenario({deferred: true, effect: 'lunar_new_year'});
  slowParticles.motion.matches = true;
  slowParticles.motion.change();
  assert.equal(slowParticles.status.textContent, 'paused');
  slowParticles.finishLoads.forEach(resolve => resolve());
  await slowParticles.settle();
  assert.equal(slowParticles.destroyed(), 3);
  assert.equal(slowParticles.status.textContent, 'paused');

  // Multiple requests while a library is loading coalesce into one final start.
  const queued = await scenario({missingLibrary: true});
  queued.nodes['dynamic-effect-retry'].click();
  queued.nodes['dynamic-effect-retry'].click();
  queued.context.tsParticles = queued.particles;
  queued.scripts[0].onload();
  await queued.settle();
  assert.equal(queued.loads.length, 1);
  assert.equal(queued.status.textContent, 'running');
  console.log('Dynamic effects: adaptive sizing, motion preferences, overrides, storage failure, resize, cleanup, load failure and retry passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
