// A small, local 3D point cloud for the account entrance. Canvas 2D keeps the login
// independent of WebGL. No frame loop survives a hidden surface or teardown.
export function mountVisionScene(root) {
  if (!root?.querySelector('canvas')) return { destroy() {} };
  const canvas = root.querySelector('canvas'), shell = root.closest('.auth-shell');
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const desktop = window.matchMedia('(min-width: 801px)');
  const forced = window.matchMedia('(forced-colors: active)');
  const pointer = window.matchMedia('(hover: hover) and (pointer: fine)');
  let disposed = false, visible = true, failed = false;
  let typing = false, resumeTimer = 0;
  let frame = 0, lastFrame = 0, elapsed = 0, width = 0, height = 0, context;
  let targetX = 0, targetY = 0, rotationX = 0, rotationY = 0, velocityX = 0, velocityY = 0;
  const buckets = Array.from({ length: 16 }, () => []);
  let colors = [];
  const cloud = [];
  for (let u = 0; u < 112; u++) for (let v = 0; v < 32; v++) {
    const a = u / 112 * Math.PI * 2, b = v / 32 * Math.PI * 2, r = .85 + .29 * Math.cos(b);
    cloud.push([r * Math.cos(a), r * Math.sin(a), .29 * Math.sin(b) + .24 * Math.sin(a * 2)]);
  }
  const status = () => disposed || !root.isConnected || document.hidden || !visible || !desktop.matches || forced.matches || !width || !height ? 'hidden'
    : failed ? 'unavailable' : reduced.matches ? 'reduced' : typing ? 'editing' : 'running';
  function readPalette() {
    const style = getComputedStyle(root);
    const read = (name, fallback) => {
      const rgb = style.getPropertyValue(name).trim().split(',').map(Number);
      return rgb.length === 3 && rgb.every(Number.isFinite) ? rgb : fallback;
    };
    const low = read('--vision-point-far', [57, 138, 112]), high = read('--vision-point-near', [211, 241, 197]);
    colors = buckets.map((_, index) => {
      const t = index / 15;
      return `rgba(${low.map((value, i) => Math.round(value + t * (high[i] - value))).join(',')},${.25 + t * .65})`;
    });
  }
  function draw() {
    if (!context) {
      try { context = canvas.getContext('2d', { alpha: true }); } catch (_) { /* CSS fallback remains visible. */ }
      if (!context) { failed = true; root.dataset.motion = 'unavailable'; return; }
    }
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    const w = Math.round(width * dpr), h = Math.round(height * dpr);
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, width, height);
    const time = elapsed / 1000;
    const a = -.65 + rotationY;
    const b = .45 + time * .22 + rotationX;
    const c = -.48 + Math.sin(time * .23) * .07;
    const ca = Math.cos(a), sa = Math.sin(a), cb = Math.cos(b), sb = Math.sin(b), cc = Math.cos(c), sc = Math.sin(c);
    const scale = Math.min(height * .37, width * .32);
    let sx, sy, depth, perspective;
    function project(x, y, z) {
      const y1 = y * ca - z * sa, z1 = y * sa + z * ca;
      const x2 = x * cb - z1 * sb, z2 = x * sb + z1 * cb;
      perspective = 3.6 / (3.6 - z2);
      sx = width / 2 + (x2 * cc - y1 * sc) * scale * perspective;
      sy = height / 2 - (x2 * sc + y1 * cc) * scale * perspective;
      depth = Math.max(0, Math.min(15, Math.floor((z2 + 1.3) / 2.6 * 16)));
    }
    buckets.forEach(bucket => { bucket.length = 0; });
    const breath = 1 + Math.sin(time * .6) * .025;
    for (const [x, y, z] of cloud) {
      project(x * breath, y * breath, z * breath);
      buckets[depth].push(sx, sy, .5 + perspective * .35);
    }
    buckets.forEach((points, index) => {
      context.fillStyle = colors[index];
      context.beginPath();
      for (let i = 0; i < points.length; i += 3) {
        context.moveTo(points[i] + points[i + 2], points[i + 1]);
        context.arc(points[i], points[i + 1], points[i + 2], 0, Math.PI * 2);
      }
      context.fill();
    });
    root.classList.add('vision-ready');
  }
  function stop() { cancelAnimationFrame(frame); frame = 0; lastFrame = 0; }
  function tick(now) {
    frame = 0;
    const state = status();
    root.dataset.motion = state;
    if (state !== 'running') { lastFrame = 0; return; }
    if (!lastFrame || now - lastFrame >= 1000 / 30 - 1) {
      const delta = lastFrame ? now - lastFrame : 0;
      const dt = Math.min(delta / 1000, .05);
      lastFrame = now; elapsed += delta;
      // Mass 1, stiffness 100, damping 10: pointer motion remains interruptible.
      velocityX += (100 * (targetX - rotationX) - 10 * velocityX) * dt;
      velocityY += (100 * (targetY - rotationY) - 10 * velocityY) * dt;
      rotationX += velocityX * dt; rotationY += velocityY * dt;
      draw();
    }
    if (!failed) frame = requestAnimationFrame(tick);
  }
  function sync() {
    stop();
    const state = status();
    root.dataset.motion = state;
    if (state === 'hidden' || state === 'unavailable') return;
    draw();
    if (state === 'running' && !failed) frame = requestAnimationFrame(tick);
  }
  function resize() {
    const rect = root.getBoundingClientRect(); width = rect.width; height = rect.height; sync();
  }
  function move(event) {
    if (status() !== 'running' || !pointer.matches) return;
    const rect = root.getBoundingClientRect();
    targetX = ((event.clientX - rect.left) / width - .5) * .6;
    targetY = ((event.clientY - rect.top) / height - .5) * .4;
  }
  function resetPointer() { targetX = targetY = 0; }
  function inputChanged(event) {
    if (!event.target.matches('.auth-form input, .auth-form textarea')) return;
    // Autofill or restored focus must not freeze the scene indefinitely. Pause
    // only while entering text, then resume even when the field keeps focus.
    clearTimeout(resumeTimer);
    typing = true; sync();
    resumeTimer = setTimeout(() => { resumeTimer = 0; if (!disposed) { typing = false; sync(); } }, 800);
  }
  function styleChanged() { if (!disposed) { readPalette(); sync(); } }
  const resizeObserver = new ResizeObserver(resize);
  const visibilityObserver = new IntersectionObserver(entries => { visible = entries[0].isIntersecting; sync(); });
  resizeObserver.observe(root); visibilityObserver.observe(root);
  root.addEventListener('pointermove', move); root.addEventListener('pointerleave', resetPointer);
  shell?.addEventListener('input', inputChanged);
  window.addEventListener('cvsprout-style-change', styleChanged);
  for (const media of [reduced, desktop, forced]) media.addEventListener('change', resize);
  document.addEventListener('visibilitychange', sync);
  readPalette(); resize();
  return {
    destroy() {
      if (disposed) return;
      disposed = true; clearTimeout(resumeTimer); stop(); resizeObserver.disconnect(); visibilityObserver.disconnect();
      root.removeEventListener('pointermove', move); root.removeEventListener('pointerleave', resetPointer);
      shell?.removeEventListener('input', inputChanged);
      window.removeEventListener('cvsprout-style-change', styleChanged);
      for (const media of [reduced, desktop, forced]) media.removeEventListener('change', resize);
      document.removeEventListener('visibilitychange', sync);
      canvas.width = canvas.height = 0;
    },
  };
}
