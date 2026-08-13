"use client";

import { useEffect, useRef } from "react";

/**
 * Real-time cel-shading of the street photographs.
 *
 * Done as a WebGL fragment shader rather than by running 431 frames through an
 * image-to-anime model: a generative pass would take minutes per frame, cost a
 * GPU we do not have, and — more importantly — would invent detail that is not
 * in the photograph. A shader restyles what is actually there, so the road,
 * traffic and signage stay exactly where the camera saw them.
 *
 * Three effects compose the look:
 * Targets the modern 3DCG anime look (Ufotable / Wit) rather than flat 1990s
 * cel fills: soft band terminators, atmospheric perspective from a depth proxy,
 * bloom on highlights, teal-and-warm grading, and outlines that thin with
 * distance. The depth cue is what makes it read as dimensional.
 */

const VERTEX = `
attribute vec2 a_pos;
varying vec2 v_uv;
void main() {
  v_uv = vec2((a_pos.x + 1.0) * 0.5, 1.0 - (a_pos.y + 1.0) * 0.5);
  gl_Position = vec4(a_pos, 0.0, 1.0);
}`;

const FRAGMENT = `
precision mediump float;
uniform sampler2D u_image;
uniform vec2 u_texel;
uniform float u_levels;
uniform float u_edge;
uniform float u_saturation;
varying vec2 v_uv;

float luma(vec3 c) { return dot(c, vec3(0.299, 0.587, 0.114)); }

void main() {
  // ---- 1. Smooth into paintable regions -------------------------------
  vec3 sum = vec3(0.0);
  for (int x = -2; x <= 2; x++) {
    for (int y = -2; y <= 2; y++) {
      sum += texture2D(u_image, v_uv + vec2(float(x), float(y)) * u_texel).rgb;
    }
  }
  vec3 base = sum / 25.0;
  vec3 raw = texture2D(u_image, v_uv).rgb;

  // ---- 2. Depth proxy --------------------------------------------------
  // No depth buffer exists for a photograph, but a forward-facing street shot
  // has strong structure: the bottom of the frame is near road surface, the
  // top is distant sky. Combined with brightness (haze lifts distant values)
  // this approximates depth well enough to drive atmospheric perspective,
  // which is what makes 3DCG anime read as dimensional rather than flat.
  float depth = clamp(1.0 - v_uv.y * 1.35, 0.0, 1.0);
  depth = mix(depth, clamp(luma(base) * 1.2, 0.0, 1.0), 0.35);

  // ---- 3. Cel bands with soft terminators ------------------------------
  float l = luma(base);
  float banded = floor(l * u_levels + 0.5) / u_levels;
  // A soft edge on each band is the difference between modern 3DCG shading
  // and 1990s flat cel fills.
  float soft = smoothstep(0.0, 0.55 / u_levels, abs(l - banded));
  float shade = mix(banded, l, soft * 0.45);
  vec3 col = base * (shade / max(l, 0.001));

  // ---- 4. Anime colour grade -------------------------------------------
  float grey = luma(col);
  col = clamp(mix(vec3(grey), col, u_saturation), 0.0, 1.0);
  // Teal shadows, warm highlights -- the standard key-art grade.
  col += vec3(-0.05, 0.01, 0.10) * (1.0 - grey);
  col += vec3(0.10, 0.05, -0.04) * pow(grey, 2.0);

  // ---- 5. Atmospheric perspective --------------------------------------
  // Distance fades toward a luminous sky tint, giving separation between the
  // near road, mid traffic and far buildings.
  vec3 haze = vec3(0.58, 0.70, 0.86);
  col = mix(col, haze, depth * 0.34);

  // ---- 6. Bloom ---------------------------------------------------------
  // Bright regions bleed light. This is the single most recognisable feature
  // of the Demon Slayer / Ufotable look.
  // Bloom, restrained. At full strength the Indian daytime sky clipped to pure
  // white and swallowed the tree line.
  vec3 bright = max(base - 0.80, 0.0);
  float glow = luma(bright);
  col += vec3(1.02, 0.96, 0.86) * glow * 0.55;

  // ---- 7. Ink outline ---------------------------------------------------
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
  // Lines thin out with distance, as in hand-inked animation.
  // Suppress outlines in very bright regions: foliage against a blown sky
  // generates thousands of tiny edges that read as scratchy noise, not ink.
  float skyMask = 1.0 - smoothstep(0.72, 0.92, l);
  float edge = smoothstep(0.16, 0.50, length(vec2(gx, gy)) * u_edge)
             * (1.0 - depth * 0.55) * skyMask;
  col = mix(col, vec3(0.07, 0.06, 0.11), edge);

  // ---- 8. Vignette ------------------------------------------------------
  vec2 d = v_uv - 0.5;
  col *= 1.0 - dot(d, d) * 0.55;

  // Give the clipped sky a painted blue instead of leaving it paper-white.
  float sky = smoothstep(0.80, 0.97, luma(base)) * smoothstep(0.55, 0.0, v_uv.y);
  col = mix(col, vec3(0.63, 0.78, 0.93), sky * 0.75);

  // Slight detail recovery so signage and vehicles stay legible.
  col = mix(col, col * 0.85 + raw * 0.15, 0.35);

  gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}`;

type Props = {
  src: string;
  levels?: number;
  edge?: number;
  saturation?: number;
  className?: string;
};

export default function AnimeFilter({
  src,
  levels = 7,
  edge = 1.05,
  saturation = 1.55,
  className = "",
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const glRef = useRef<WebGLRenderingContext | null>(null);
  const progRef = useRef<WebGLProgram | null>(null);
  const texRef = useRef<WebGLTexture | null>(null);

  // Compile once; the shader is reused for every frame of the drive-through.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const gl = canvas.getContext("webgl", { preserveDrawingBuffer: false });
    if (!gl) return;
    glRef.current = gl;

    const compile = (type: number, source: string) => {
      const shader = gl.createShader(type)!;
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        // Silent shader failures render a black canvas with no clue why.
        console.error("[anime] shader compile failed:", gl.getShaderInfoLog(shader));
      }
      return shader;
    };
    const program = gl.createProgram()!;
    gl.attachShader(program, compile(gl.VERTEX_SHADER, VERTEX));
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, FRAGMENT));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      console.error("[anime] program link failed:", gl.getProgramInfoLog(program));
    }
    gl.useProgram(program);
    progRef.current = program;

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
    texRef.current = texture;

    return () => {
      gl.deleteProgram(program);
      gl.deleteTexture(texture);
    };
  }, []);

  // Redraw whenever the frame changes.
  useEffect(() => {
    const canvas = canvasRef.current;
    const gl = glRef.current;
    const program = progRef.current;
    if (!canvas || !gl || !program) return;

    let cancelled = false;
    const image = new Image();
    image.crossOrigin = "anonymous";
    image.onerror = () => console.error("[anime] image failed to load", src);
    image.onload = () => {
      if (cancelled) return;
      // Cap the working resolution: the 5x5 blur is the expensive part and
      // 1280px is indistinguishable at playback size.
      const scale = Math.min(1, 1280 / image.naturalWidth);
      canvas.width = Math.round(image.naturalWidth * scale);
      canvas.height = Math.round(image.naturalHeight * scale);

      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.bindTexture(gl.TEXTURE_2D, texRef.current);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);

      gl.uniform2f(
        gl.getUniformLocation(program, "u_texel"),
        1.0 / canvas.width,
        1.0 / canvas.height,
      );
      gl.uniform1f(gl.getUniformLocation(program, "u_levels"), levels);
      gl.uniform1f(gl.getUniformLocation(program, "u_edge"), edge);
      gl.uniform1f(gl.getUniformLocation(program, "u_saturation"), saturation);
      gl.drawArrays(gl.TRIANGLES, 0, 6);
      const err = gl.getError();
      if (err !== gl.NO_ERROR) console.error("[anime] gl error", err);
    };
    image.src = src;
    return () => {
      cancelled = true;
    };
  }, [src, levels, edge, saturation]);

  return <canvas ref={canvasRef} className={className} />;
}
