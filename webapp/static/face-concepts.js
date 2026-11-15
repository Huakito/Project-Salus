import { FONT_5X7 } from "./font5x7.js";

const WIDTH = 160;
const HEIGHT = 80;
const SCALE = 4;
const GREEN = "#67ed9b";
const RED = "#ff5c70";
const GLYPHS = Uint8Array.from(FONT_5X7.match(/../g), (hex) => parseInt(hex, 16));

function begin(canvas) {
  const ctx = canvas.getContext("2d");
  ctx.setTransform(SCALE, 0, 0, SCALE, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, WIDTH, HEIGHT);
  return ctx;
}

function px(ctx, colour, x, y, width, height) {
  ctx.fillStyle = colour;
  ctx.fillRect(Math.round(x), Math.round(y), width, height);
}

function pattern(ctx, shape, x, y, scale, colour) {
  shape.forEach((row, rowIndex) => {
    [...row].forEach((cell, columnIndex) => {
      if (cell === "1") px(ctx, colour, x + columnIndex * scale, y + rowIndex * scale, scale, scale);
    });
  });
}

const HEART = [
  "0110110",
  "1111111",
  "1111111",
  "0111110",
  "0011100",
  "0001000",
];

function drawLine(ctx, text, left, top, size, colour = GREEN) {
  ctx.fillStyle = colour;
  for (let column = 0; column < text.length; column += 1) {
    const glyph = (text.charCodeAt(column) - 0x20) * 5;
    for (let x = 0; x < 5; x += 1) {
      for (let y = 0; y < 8; y += 1) {
        if (GLYPHS[glyph + x] & (1 << y)) {
          px(ctx, colour, left + (column * 6 + x) * size, top + y * size, size, size);
        }
      }
    }
  }
}

function textFace(ctx, text, size = 3) {
  const width = text.length * 6 * size;
  drawLine(ctx, text, Math.floor((WIDTH - width) / 2), Math.floor((HEIGHT - 8 * size) / 2), size);
}

function heartFace(ctx) {
  textFace(ctx, "(^_^)", 3);
  pattern(ctx, HEART, 6, 29, 3, RED);
  pattern(ctx, HEART, 133, 29, 3, RED);
}

const concepts = [
  {name: "Little hearts", use: "The original happy face with real red pixel hearts", draw: heartFace},
  {name: "Cheeky wink", use: "A small wink for playful prompts", text: "(^_~)b"},
  {name: "Delighted", use: "An excited response after something goes well", text: "\\(^o^)/"},
  {name: "Curious", use: "A questioning look while waiting for an answer", text: "(o_O)?"},
  {name: "Proud", use: "A simple approving expression", text: "(^-^)b"},
  {name: "Surprised", use: "A wide-eyed reaction for unexpected moments", text: "(O_O)!"},
];

const cards = [...document.querySelectorAll("article")];
cards.forEach((card, index) => {
  const concept = concepts[index];
  card.querySelector("h2").textContent = `${index + 1}. ${concept.name}`;
  card.querySelector("p").textContent = concept.use;
});

cards.forEach((card, index) => {
  const concept = concepts[index];
  const ctx = begin(card.querySelector("canvas"));
  if (concept.draw) concept.draw(ctx);
  else textFace(ctx, concept.text);
});
