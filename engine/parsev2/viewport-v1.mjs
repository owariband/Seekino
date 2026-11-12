// Import cameraAt, then draw the returned source rectangle:
// const r = cameraAt(elapsedMs, params, image, ctx.canvas);
// ctx.drawImage(image, r.x, r.y, r.width, r.height, 0, 0, ctx.canvas.width, ctx.canvas.height);
// world and viewport take {width, height}. Each anchor is [horizontal, vertical, zoom].
// 0 and 1 select opposite edges of available travel. Choose anchors for your artwork.
function measuredProgress(u, points, easing) {
  // A shape-preserving cubic joins the sampled positions with continuous velocity.
  // Source sample times are scaled to the duration of this new shot.
  const last = points.length - 1;
  if (last < 1 || points[0][0] !== 0 || points[0][1] !== 0 ||
      points[last][0] !== 1 || points[last][1] !== 1) {
    throw new RangeError("Progress samples must start at [0,0] and end at [1,1]");
  }
  let segment = 0;
  for (let i = 1; i <= last; i++) {
    const [time, position] = points[i];
    if (!Number.isFinite(time) || !Number.isFinite(position) ||
        time <= points[i - 1][0] || position < points[i - 1][1]) {
      throw new RangeError("Sample times must increase and positions must be monotone");
    }
    if (time <= u && i < last) segment = i;
  }
  const span = i => points[i + 1][0] - points[i][0];
  const slope = i => (points[i + 1][1] - points[i][1]) / span(i);
  function tangent(i) {
    if (i === 0) return easing === "EASE_IN" || easing === "EASE_IN_OUT" ? 0 : slope(0);
    if (i === last) return easing === "EASE_OUT" || easing === "EASE_IN_OUT" ? 0 : slope(last - 1);
    const a = slope(i - 1), b = slope(i);
    if (a === 0 || b === 0) return 0;
    const before = span(i - 1), after = span(i);
    return 3 * (before + after) / ((2 * after + before) / a + (after + 2 * before) / b);
  }
  const width = span(segment);
  const s = (u - points[segment][0]) / width;
  const v = 1 - s;
  const position = points[segment][1];
  return position + s * s * (3 - 2 * s) * (points[segment + 1][1] - position)
    + width * s * v * (v * tangent(segment) - s * tangent(segment + 1));
}

export function cameraAt(elapsedMs, params, world, viewport) {
  const {duration_ms, easing, from, to} = params;
  const positive = n => Number.isFinite(n) && n > 0;
  if (!Number.isFinite(elapsedMs) || ![duration_ms, world.width, world.height,
      viewport.width, viewport.height].every(positive)) {
    throw new RangeError("Invalid time or dimensions");
  }
  for (const anchor of [from, to]) {
    if (!Array.isArray(anchor) || anchor.length !== 3 || !anchor.every(Number.isFinite) ||
        anchor[0] < 0 || anchor[0] > 1 || anchor[1] < 0 || anchor[1] > 1 || anchor[2] < 1) {
      throw new RangeError("An anchor requires x and y in [0,1] and zoom >= 1");
    }
  }
  const u = Math.max(0, Math.min(1, elapsedMs / duration_ms));
  let p;
  switch (easing) {
    case "STEADY": p = u; break;
    case "EASE_IN": p = 1 - Math.cos(u * Math.PI / 2); break;
    case "EASE_OUT": p = Math.sin(u * Math.PI / 2); break;
    case "EASE_IN_OUT": p = (1 - Math.cos(u * Math.PI)) / 2; break;
    default: throw new RangeError("Unsupported easing");
  }
  if (params.progress_samples) p = measuredProgress(u, params.progress_samples, easing);
  const aspect = viewport.width / viewport.height;
  const fitWidth = Math.min(world.width, world.height * aspect);
  const fitHeight = fitWidth / aspect;
  if (from[2] === 1 && to[2] === 1 &&
      ((from[0] !== to[0] && world.width - fitWidth < 1e-8) ||
       (from[1] !== to[1] && world.height - fitHeight < 1e-8))) {
    throw new RangeError("Provide more artwork along the pan axis or increase the anchor zoom");
  }
  const zoom = from[2] * (to[2] / from[2]) ** p;
  const width = fitWidth / zoom, height = fitHeight / zoom;
  let x = (world.width - width) * (from[0] + (to[0] - from[0]) * p);
  if (params.travel_in_view_widths !== undefined) {
    const travel = params.travel_in_view_widths;
    if (!positive(travel) || from[2] !== to[2] || from[0] === to[0]) {
      throw new RangeError("Sampled travel requires a horizontal pan at fixed zoom");
    }
    const start = (world.width - width) * from[0];
    const delta = Math.sign(to[0] - from[0]) * travel * width;
    if (start + delta < -1e-8 || start + delta > world.width - width + 1e-8) {
      throw new RangeError("The composition is too narrow for the requested travel in viewport widths");
    }
    x = start + p * delta;
  }
  return {
    x,
    y: (world.height - height) * (from[1] + (to[1] - from[1]) * p),
    width, height,
  };
}
