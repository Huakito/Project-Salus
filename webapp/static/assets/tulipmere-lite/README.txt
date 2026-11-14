TULIPMERE VILLAGE LITE - Free 16x16 Spring Village Starter Pack

Contents
  tulipmere_village_lite_tileset_16x16.png   Tileset sheet, 160x48 px (10x3 tiles, no spacing)
  tulipmere_village_lite_tileset_16x16.json  Position of every tile on the sheet
  objects/                               Whole sprites, one PNG per object larger than one tile (1 file)
  tiles/                                 Every single 16x16 tile as a separate PNG (26 files)
  tiles/split/                           The same large objects as 16x16 tiles, named <name>_<row>_<col>.png, for tile maps (4 files)
  characters/                                gardener_spritesheet.png (64x96 px, walk in 4 directions) + characters.json
  items/                                     strawberry, tulip and herbal tea: one 48x16 px sheet, 3 PNG files + items.json
  preview-lite.png                           Example scene (shown at 4x)
  walk-lite.gif                              The gardener's walk cycle (shown at 6x)

How to use
  Tile size is 16x16. Import the sheet with no margin and no spacing.
  path_* is a 3x3 dirt path set on grass. water_round_* makes a small round pond on grass.
  strawberry_stage0-3 are transparent overlays for the soil tile.
  Objects larger than one tile are in objects/ as one PNG each: cherry tree 32x32 (2x2).
    The tileset JSON still lists the individual tiles of each object on the sheet.
    The individual 16x16 tiles of those objects are in tiles/split/, named <name>_<row>_<col>.png, for tile maps.
  The gardener has 16x24 px frames: rows 0-3 walk down, up, left, right. Frames: 0 stand, 1 step, 2 stand, 3 step. About 8 fps.
  Scale by whole numbers only with nearest-neighbour filtering.

Want more? The full Tulipmere Village pack has 180 tiles with an animated pond, 3 autotile sets, three cottages, a windmill
with turning sails, a flower stall, a garden shed, 4 crops with growth stages, 21 props, a florist, a beekeeper, a duck,
a rabbit, butterflies, dig and sow animations for the gardener and 16 items.

See LICENSE.txt for the terms of use.
