/**
 * Blueprint for Lumi's 7x7 house, as an ordered list of cells.
 *
 * Pure (no bot access), so it can be checked without a Minecraft server.
 * bot.js walks the list in order and skips cells that are already right,
 * which makes building resumable: each run continues where the last one
 * stopped.
 *
 * Layout, with (x0, y0, z0) the floor's north-west corner and the front
 * wall on the z0 side:
 *   y0        floor, 7x7 (replaces the ground)
 *   y0+1..+3  walls on the 7x7 perimeter, 3 high
 *               door: 1x2 gap in the middle of the front wall
 *               windows: 1x1 at y0+2 in the middle of the other walls
 *   y0+4      flat roof, 7x7, placed from the edge inward so each block
 *             has a placed neighbour to go against
 * Inside the walls is cleared to air first, so the house can sit against
 * a slope.
 */

"use strict";

const SIZE = 7;
const MID = (SIZE - 1) / 2;
const WALL_HEIGHT = 3;

// The perimeter in walking order, so each wall block sits next to the
// previous one.
function perimeter() {
  const out = [];
  for (let dx = 0; dx < SIZE; dx++) out.push([dx, 0]);
  for (let dz = 1; dz < SIZE; dz++) out.push([SIZE - 1, dz]);
  for (let dx = SIZE - 2; dx >= 0; dx--) out.push([dx, SIZE - 1]);
  for (let dz = SIZE - 2; dz >= 1; dz--) out.push([0, dz]);
  return out;
}

function wallKind(dx, dy, dz) {
  if (dz === 0 && dx === MID && dy <= 2) return dy === 1 ? "door" : "door_top";
  if (dy === 2 && ((dz === MID && (dx === 0 || dx === SIZE - 1)) || (dz === SIZE - 1 && dx === MID))) {
    return "window";
  }
  return "wall";
}

function houseBlueprint(x0, y0, z0) {
  const cells = [];
  const at = (kind, dx, dy, dz) => cells.push({ kind, x: x0 + dx, y: y0 + dy, z: z0 + dz });

  for (let dy = 1; dy <= WALL_HEIGHT; dy++)
    for (let dx = 1; dx < SIZE - 1; dx++)
      for (let dz = 1; dz < SIZE - 1; dz++) at("air", dx, dy, dz);

  for (let dx = 0; dx < SIZE; dx++)
    for (let dz = 0; dz < SIZE; dz++) at("floor", dx, 0, dz);

  for (let dy = 1; dy <= WALL_HEIGHT; dy++)
    for (const [dx, dz] of perimeter()) at(wallKind(dx, dy, dz), dx, dy, dz);

  for (let ring = 0; ring <= MID; ring++)
    for (let dx = 0; dx < SIZE; dx++)
      for (let dz = 0; dz < SIZE; dz++)
        if (Math.max(Math.abs(dx - MID), Math.abs(dz - MID)) === MID - ring) {
          at("roof", dx, WALL_HEIGHT + 1, dz);
        }

  at("torch_inside", 1, 1, 1);
  at("torch_inside", SIZE - 2, 1, SIZE - 2);
  // Outside, beside the door. Its height is found at build time, since the
  // ground in front of the house may not be level with the floor.
  at("torch_outside", MID - 1, 1, -1);
  return cells;
}

// Cells that take the main building material.
const STRUCTURAL = new Set(["floor", "wall", "roof"]);

function materialNeeded(cells) {
  return cells.filter(c => STRUCTURAL.has(c.kind)).length;
}

module.exports = { houseBlueprint, materialNeeded, STRUCTURAL, HOUSE_SIZE: SIZE };
