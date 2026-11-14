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

// Start drawing on a canvas that stands for the screen: black, and scaled so
// that one unit is one screen pixel.
function blankScreen(canvas, screen) {
  const ctx = canvas.getContext("2d");
  const zoom = canvas.width / screen.width; // the canvas has several pixels per screen pixel
  ctx.setTransform(zoom, 0, 0, zoom, 0, 0);
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, screen.width, screen.height);
  return ctx;
}

// One line of text in the screen's font, its top left corner at (left, top),
// every font pixel a square `size` screen pixels wide.
function drawLine(ctx, line, left, top, size) {
  for (let col = 0; col < line.length; col++) {
    const glyph = (line.charCodeAt(col) - 0x20) * 5;
    for (let x = 0; x < 5; x++) {
      for (let y = 0; y < 8; y++) {
        if (GLYPHS[glyph + x] & (1 << y)) {
          ctx.fillRect(left + (col * CELL_WIDTH + x) * size, top + y * size, size, size);
        }
      }
    }
  }
}

// Draw the face as the firmware will: black background, the block of text
// centred.
export function drawFace(canvas, face, screen, cssColour) {
  const ctx = blankScreen(canvas, screen);
  if (face.empty || face.problem) return;

  const { lines, cols, rows, size } = face;
  const left = Math.floor((screen.width - cols * CELL_WIDTH * size) / 2);
  const top = Math.floor((screen.height - rows * CELL_HEIGHT * size) / 2);
  ctx.fillStyle = cssColour;
  lines.forEach((line, row) => drawLine(ctx, line, left, top + row * CELL_HEIGHT * size, size));
}

// Draw the volume screen the robot shows when its volume changes. The layout
// is the same sums as drawVolume() in firmware/project.yaml, where the words
// sit nearer the top than the bar does the bottom. Here, and only here, the
// picture is drawn a little smaller and moved so it sits in the middle.
// `percent` is 0 to 100.
export function drawVolume(canvas, screen, percent) {
  const ctx = blankScreen(canvas, screen);
  const { width, height } = screen;
  const size = Math.max(1, Math.min(Math.floor(width / 80), Math.floor(height / 40)));
  const label = `Volume ${percent}%`;
  const labelWidth = label.length * CELL_WIDTH * size;
  const labelLeft = Math.floor((width - labelWidth) / 2);
  const labelTop = Math.floor(height / 10);
  const x = Math.floor(width / 16);
  const y = Math.floor(height / 2);
  const barWidth = width - 2 * x;
  const barHeight = Math.floor(height / 3);

  // One canvas pixel fewer for each screen pixel (a whole number, so the
  // font's squares stay sharp), with the words and the bar centred as a block.
  const zoom = canvas.width / screen.width;
  const unit = Math.max(1, Math.round(zoom) - 1);
  const left = Math.min(labelLeft, x);
  const right = Math.max(labelLeft + labelWidth, x + barWidth);
  const bottom = y + barHeight;
  ctx.setTransform(
    unit,
    0,
    0,
    unit,
    Math.round((canvas.width - unit * (left + right)) / 2),
    Math.round((canvas.height - unit * (labelTop + bottom)) / 2),
  );

  ctx.fillStyle = "#fff";
  drawLine(ctx, label, labelLeft, labelTop, size);
  // The outline of the bar, one screen pixel thick, then its cyan filling.
  ctx.fillRect(x, y, barWidth, 1);
  ctx.fillRect(x, y + barHeight - 1, barWidth, 1);
  ctx.fillRect(x, y, 1, barHeight);
  ctx.fillRect(x + barWidth - 1, y, 1, barHeight);
  ctx.fillStyle = "rgb(0, 255, 255)";
  ctx.fillRect(x + 2, y + 2, Math.floor(((barWidth - 4) * percent) / 100), barHeight - 4);
}
