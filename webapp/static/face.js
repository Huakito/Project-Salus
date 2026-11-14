// What the robot's screen can show as a face, and how it will look there.
//
// The same rules exist in three places: here (for the live preview), in
// salus/face.py (the server checks again before sending) and in the firmware
// (which does the drawing). Change one, change all three.

import { FONT_5X7 } from "./font5x7.js";

export const CELL_WIDTH = 6; // pixels per character at text size 1
export const CELL_HEIGHT = 8;

const GLYPHS = Uint8Array.from(FONT_5X7.match(/../g), (hex) => parseInt(hex, 16));

// Split the art into lines, dropping what would only push it off-centre:
// spaces at the ends of lines, and blank lines above and below.
export function faceLines(text) {
  const lines = text.replace(/\r\n?/g, "\n").split("\n").map((line) => line.replace(/ +$/, ""));
  while (lines.length && lines[0] === "") lines.shift();
  while (lines.length && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

// Work out how the art sits on a screen of the given size. `problem` says why
// it cannot be shown, if it cannot.
export function checkFace(text, screen) {
  const lines = faceLines(text);
  const rows = lines.length;
  const cols = Math.max(0, ...lines.map((line) => line.length));
  const face = { lines, cols, rows, size: 0, empty: rows === 0, problem: null };
  if (face.empty) return face;

  const fitsAcross = Math.floor(screen.width / CELL_WIDTH);
  const fitsDown = Math.floor(screen.height / CELL_HEIGHT);
  const strange = [...new Set(lines.join(""))].filter((ch) => ch < " " || ch > "~");
  if (strange.length) {
    const shown = strange.slice(0, 8).map((ch) => (ch === "\t" ? "Tab" : ch)).join(" ");
    face.problem = `The screen can only draw plain keyboard characters. It can't draw: ${shown}`;
  } else if (cols > fitsAcross) {
    face.problem = `Too wide: the longest line has ${cols} characters and the screen fits ${fitsAcross}.`;
  } else if (rows > fitsDown) {
    face.problem = `Too tall: this has ${rows} lines and the screen fits ${fitsDown}.`;
  } else {
    // The biggest text size at which the whole block still fits.
    face.size = Math.min(
      Math.floor(screen.width / (CELL_WIDTH * cols)),
      Math.floor(screen.height / (CELL_HEIGHT * rows)),
    );
  }
  return face;
}

// "#rrggbb" to the 16-bit colour the screen uses: 5 bits of red, 6 of green,
// 5 of blue. "#00ff00" becomes 2016.
export function rgb565(hex) {
  const rgb = parseInt(hex.slice(1), 16);
  const red = rgb >> 16;
  const green = (rgb >> 8) & 0xff;
  const blue = rgb & 0xff;
  return ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3);
}

// The colour the screen will really show: it has fewer shades than a monitor.
export function rgb565ToCss(colour) {
  const red = Math.round((((colour >> 11) & 0x1f) * 255) / 31);
  const green = Math.round((((colour >> 5) & 0x3f) * 255) / 63);
  const blue = Math.round(((colour & 0x1f) * 255) / 31);
  return `rgb(${red}, ${green}, ${blue})`;
}

// Draw the face as the firmware will: black background, the block of text
// centred, every font pixel a square `size` screen pixels wide.
export function drawFace(canvas, face, screen, cssColour) {
  const ctx = canvas.getContext("2d");
  const zoom = canvas.width / screen.width; // the canvas has several pixels per screen pixel
  ctx.setTransform(zoom, 0, 0, zoom, 0, 0);
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, screen.width, screen.height);
  if (face.empty || face.problem) return;

  const { lines, cols, rows, size } = face;
  const left = Math.floor((screen.width - cols * CELL_WIDTH * size) / 2);
  const top = Math.floor((screen.height - rows * CELL_HEIGHT * size) / 2);
  ctx.fillStyle = cssColour;
  lines.forEach((line, row) => {
    for (let col = 0; col < line.length; col++) {
      const glyph = (line.charCodeAt(col) - 0x20) * 5;
      for (let x = 0; x < 5; x++) {
        for (let y = 0; y < 8; y++) {
          if (GLYPHS[glyph + x] & (1 << y)) {
            ctx.fillRect(
              left + (col * CELL_WIDTH + x) * size,
              top + (row * CELL_HEIGHT + y) * size,
              size,
              size,
            );
          }
        }
      }
    }
  });
}
