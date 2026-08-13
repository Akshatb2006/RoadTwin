/**
 * Continuous cel-shading of a live WebGL canvas.
 *
 * Used to stylise the Gaussian splat render. The reconstruction is weakly
 * constrained by forward-only imagery, so as "photorealism" it reads as broken.
 * Stylised, the same softness reads as art direction — and unlike a photograph
 * sequence it is still a real 3D scene, so the camera can move through it with
 * genuine depth and parallax.
 *
 * The source canvas must be created with preserveDrawingBuffer: true, or there
 * is nothing to sample by the time this runs.
 */

const VERTEX = `
attribute vec2 a_pos;
varying vec2 v_uv;
void main() {
  v_uv = vec2((a_pos.x + 1.0) * 0.5, (a_pos.y + 1.0) * 0.5);
  gl_Position = vec4(a_pos, 0.0, 1.0);
}`;

const FRAGMENT = `
precision mediump float;
uniform sampler2D u_image;
uniform vec2 u_texel;
varying vec2 v_uv;

float luma(vec3 c) { return dot(c, vec3(0.299, 0.587, 0.114)); }

void main() {
  vec3 sum = vec3(0.0);
  for (int x = -2; x <= 2; x++) {
    for (int y = -2; y <= 2; y++) {
      sum += texture2D(u_image, v_uv + vec2(float(x), float(y)) * u_texel).rgb;
    }
  }
  vec3 base = sum / 25.0;

  // Splat renders are already soft, so quantise harder than a photograph needs:
  // fewer, flatter bands make the softness look chosen rather than accidental.
  float l = luma(base);
  float levels = 5.0;
  float banded = floor(l * levels + 0.5) / levels;
  float soft = smoothstep(0.0, 0.5 / levels, abs(l - banded));
  float shade = mix(banded, l, soft * 0.35);
  vec3 col = base * (shade / max(l, 0.001));

  float grey = luma(col);
  col = clamp(mix(vec3(grey), col, 1.7), 0.0, 1.0);
  col += vec3(-0.04, 0.01, 0.09) * (1.0 - grey);
  col += vec3(0.10, 0.05, -0.03) * pow(grey, 2.0);

  // Depth proxy from frame geometry: bottom is near road, top is distance.
  float depth = clamp(1.0 - v_uv.y * 1.3, 0.0, 1.0);
  col = mix(col, vec3(0.60, 0.72, 0.88), depth * 0.30);

  vec3 bright = max(base - 0.78, 0.0);
  col += vec3(1.0, 0.95, 0.85) * luma(bright) * 0.6;

  float tl = luma(texture2D(u_image, v_uv + vec2(-1.0, -1.0) * u_texel).rgb);
  float tc = luma(texture2D(u_image, v_uv + vec2( 0.0, -1.0) * u_texel).rgb);
  float tr = luma(texture2D(u_image, v_uv + vec2( 1.0, -1.0) * u_texel).rgb);
  float ml = luma(texture2D(u_image, v_uv + vec2(-1.0,  0.0) * u_texel).rgb);
  float mr = luma(texture2D(u_image, v_uv + vec2( 1.0,  0.0) * u_texel).rgb);
  float bl = luma(texture2D(u_image, v_uv + vec2(-1.0,  1.0) * u_texel).rgb);
  float bc = luma(texture2D(u_image, v_uv + vec2( 0.0,  1.0) * u_texel).rgb);
  float br = luma(texture2D(u_image, v_uv + vec2( 1.0,  1.0) * u_texel).rgb);
  float gx = -tl - 2.0 * ml - bl + tr + 2.0 * mr + br;
  float gy = -tl - 2.0 * tc - tr + bl + 2.0 * bc + br;
  // A generous threshold: splat noise would otherwise be inked as detail.
  float edge = smoothstep(0.22, 0.60, length(vec2(gx, gy))) * (1.0 - depth * 0.5);
  col = mix(col, vec3(0.06, 0.05, 0.10), edge);

  vec2 d = v_uv - 0.5;
  col *= 1.0 - dot(d, d) * 0.5;

  gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}`;

export function attachAnimeFilter(
  source: HTMLCanvasElement,
  target: HTMLCanvasElement,
): () => void {
  const gl = target.getContext("webgl");
  if (!gl) return () => {};

  const compile = (type: number, src: string) => {
    const shader = gl.createShader(type)!;
    gl.shaderSource(shader, src);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      console.error("[anime3d] compile:", gl.getShaderInfoLog(shader));
    }
    return shader;
  };

  const program = gl.createProgram()!;
  gl.attachShader(program, compile(gl.VERTEX_SHADER, VERTEX));
  gl.attachShader(program, compile(gl.FRAGMENT_SHADER, FRAGMENT));
  gl.linkProgram(program);
  gl.useProgram(program);

  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(
    gl.ARRAY_BUFFER,
    new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
    gl.STATIC_DRAW,
  );
  const loc = gl.getAttribLocation(program, "a_pos");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

  const texture = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);

  let raf = 0;
  let stopped = false;

  const frame = () => {
    if (stopped) return;
    raf = requestAnimationFrame(frame);
    if (!source.width || !source.height) return;

    if (target.width !== source.width || target.height !== source.height) {
      target.width = source.width;
      target.height = source.height;
    }
    gl.viewport(0, 0, target.width, target.height);
    try {
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
    } catch {
      return; // source not renderable yet
    }
    gl.uniform2f(
      gl.getUniformLocation(program, "u_texel"),
      1 / target.width,
      1 / target.height,
    );
    gl.drawArrays(gl.TRIANGLES, 0, 6);
  };
  raf = requestAnimationFrame(frame);

  return () => {
    stopped = true;
    cancelAnimationFrame(raf);
    gl.deleteProgram(program);
    gl.deleteTexture(texture);
  };
}
