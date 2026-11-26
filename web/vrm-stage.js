/* ============================================================
   The VRM avatar on the web client (Phase 3, Stage).
   Loads three.js and @pixiv/three-vrm from web/vendor (offline; run
   `npm install` in web/vendor once), the model from /api/avatar/model
   (data/avatars/lumi.vrm on this PC), and runs a light idle: head sway,
   breathing and blinks. Renders at most 30 fps, and only while the
   canvas is on screen and the tab is visible.

   mount(host) -> a cleanup function. Without a model, the host shows
   where to put one. Touch and emotion are not wired yet.
   ============================================================ */

let libs = null;

function loadLibs() {
  if (!libs) {
    libs = Promise.all([
      import('three'),
      import('@pixiv/three-vrm'),
      import('three/addons/loaders/GLTFLoader.js'),
    ]).then(([THREE, vrm, gl]) => ({ THREE, ...vrm, GLTFLoader: gl.GLTFLoader }));
  }
  return libs;
}

function note(host, text) {
  const p = document.createElement('p');
  p.className = 'stage-note';
  p.textContent = text;
  host.replaceChildren(p);
  return () => {};
}

export async function mount(host) {
  if (!host) return () => {};
  host.replaceChildren(Object.assign(document.createElement('p'), { className: 'stage-note', textContent: 'Loading the avatar…' }));

  let status;
  try {
    const r = await fetch('/api/avatar/status', { credentials: 'same-origin', cache: 'no-store' });
    if (!r.ok) return note(host, 'Sign in on the host PC to see the avatar.');
    status = await r.json();
  } catch (_) {
    return note(host, 'Lumi’s web server isn’t reachable.');
  }
  if (!status.model) {
    return note(host, 'No avatar yet. Put a VRM model at data\\avatars\\lumi.vrm on this PC, then reload.');
  }
  if (!window.WebGL2RenderingContext) return note(host, 'This browser has no WebGL 2, so the avatar can’t be drawn.');

  let lib;
  try {
    lib = await loadLibs();
  } catch (_) {
    return note(host, 'The avatar libraries are missing. Run npm install in web\\vendor on this PC.');
  }
  const { THREE, VRMLoaderPlugin, VRMUtils, GLTFLoader } = lib;

  const w = Math.max(200, host.clientWidth || 320);
  const h = Math.max(200, host.clientHeight || 360);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
  renderer.setSize(w, h);
  host.replaceChildren(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(28, w / h, 0.1, 20);
  camera.position.set(0, 1.25, 2.7);
  camera.lookAt(0, 1.15, 0);
  scene.add(new THREE.AmbientLight(0xffffff, 0.7));
  const key = new THREE.DirectionalLight(0xffffff, 2.0);
  key.position.set(1, 1.5, 1.5);
  scene.add(key);

  let vrm = null;
  let alive = true;
  const dispose = () => {
    alive = false;
    renderer.setAnimationLoop(null);
    ro.disconnect();
    document.removeEventListener('visibilitychange', onVis);
    renderer.dispose();
  };

  const ro = new ResizeObserver(() => {
    if (!host.isConnected) return dispose();
    const nw = Math.max(200, host.clientWidth), nh = Math.max(200, host.clientHeight);
    renderer.setSize(nw, nh);
    camera.aspect = nw / nh;
    camera.updateProjectionMatrix();
  });
  ro.observe(host);

  try {
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));
    const gltf = await loader.loadAsync('/api/avatar/model');
    vrm = gltf.userData.vrm;
    if (!vrm) { dispose(); return note(host, 'That file isn’t a VRM model.'); }
    VRMUtils.removeUnnecessaryJoints?.(gltf.scene);
    if (vrm.meta?.metaVersion === '0') VRMUtils.rotateVRM0?.(vrm);
    scene.add(vrm.scene);
  } catch (_) {
    dispose();
    return note(host, 'The avatar couldn’t be loaded. Check the file is a valid VRM.');
  }

  const head = vrm.humanoid?.getNormalizedBoneNode('head');
  const chest = vrm.humanoid?.getNormalizedBoneNode('chest') || vrm.humanoid?.getNormalizedBoneNode('spine');
  const clock = new THREE.Clock();
  let blinkStart = -1, nextBlink = 2 + Math.random() * 2, last = 0;
  const FRAME_MS = 1000 / 30;

  const frame = (now) => {
    if (!alive) return;
    if (now - last < FRAME_MS) return;                  // 30 fps cap
    last = now;
    const dt = Math.min(clock.getDelta(), 0.1);
    const s = clock.elapsedTime;
    if (head) {                                          // layered sines: never quite repeats
      head.rotation.y = 0.05 * Math.sin(s * 0.5) + 0.02 * Math.sin(s * 1.7 + 1);
      head.rotation.x = 0.03 * Math.sin(s * 0.7 + 2);
      head.rotation.z = 0.02 * Math.sin(s * 0.37);
    }
    if (chest) chest.rotation.x = 0.015 * Math.sin(s * 1.3);        // breathing
    if (blinkStart < 0 && s >= nextBlink) blinkStart = s;
    let blink = 0;
    if (blinkStart >= 0) {
      const k = (s - blinkStart) / 0.16;
      if (k >= 1) { blinkStart = -1; nextBlink = s + 2.5 + Math.random() * 4; }
      else blink = Math.sin(Math.PI * k);
    }
    vrm.expressionManager?.setValue('blink', blink);
    vrm.update(dt);
    renderer.render(scene, camera);
  };

  const onVis = () => {
    if (document.hidden) renderer.setAnimationLoop(null);
    else renderer.setAnimationLoop(frame);
  };
  document.addEventListener('visibilitychange', onVis);
  renderer.setAnimationLoop(frame);
  return dispose;
}
