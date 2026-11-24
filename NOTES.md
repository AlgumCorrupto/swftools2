# MC3 (PS2) .pck <-> .swf : tools and research notes

## Usage (Python 3, Pillow + numpy only needed for bitmap re-quantizing / selftest)
    python3 pck2swf.py legal.pck legal.swf [--zlib] [--no-embed]
    python3 swf2pck.py legal.swf legal_new.pck [--template legal.pck] [--force]
`pck2swf` embeds the source pck (zlib+base64) in a SWF Metadata tag (77, "RPCK1:"), so `swf2pck` needs no
template. Without embedding, pass `--template`. CharacterId == object index in the pck list.

## What swf2pck does
Append-only patching of the template: objects whose SWF definition is unchanged are untouched (fonts, buttons,
edit text, text keep their engine-only data). Changed SHAPE / SPRITE / root timeline are re-emitted at the end of the
data block (list pointer + count updated; button record pointers fixed up). Changed BITMAPs of the same size and
bit depth are re-swizzled and patched in place (32-bit input is quantized to the original palette size).
Not supported (warned, left unchanged): new/resized bitmaps, edits to FONT/TEXT/EDITTEXT/BUTTON, radial gradients
(approximated linear), new characters other than SHAPE/SPRITE.

## Verified (selftest.py, all 4 samples)
no-edit round trip is byte-identical; forced re-emission of every shape/sprite/root reparses with identical
sprite timelines, identical bitmap pixels, shape coverage IoU >= 0.992 (triangulated vs original polygons).
NOT verified: loading in the game / a Flash player (no emulator or Flash runtime was available).

## Format (PS2 build; confirms and extends the RAGE-SWF-research notes)
- 0x80 header: u32 base, u32 6, u32 1, u32 dataSize; data = C++ object dump, absolute pointers (base+offset).
- Root swfFILE (0x30): +4 vtable, +8 u16 frame count, +0xC frames*, +0x10 object ptr list*, +0x14 u8 (6, unknown),
  +0x18..0x27 float xmin,xmax,ymin,ymax (px; 0,640,0,480), +0x28 u16 frame rate 8.8 fixed (0x4600 = 70 fps),
  +0x2A u16 object count. Frame = {u32 aux, u32 firstCmd}; root frame aux = RGBA bytes of the stage background.
- Object header: u8 type (1 shape, 2 sprite, 3 button, 4 bitmap, 5 font, 6 text, 7 edittext), +4 vtable (overwritten by loader).
- Commands (linked list): +0 type (0 Place, 1 PlaceClip, 2 Remove, 3 DoAction), +1 matrix flags (bit0 scale, bit1 rotate;
  0xCD = no matrix), +4 next, +8 vtable. Place: +0xC depth u16, +0xE char u16 (0xFFFF = modify existing), +0x10 packed
  matrix*, +0x14 cxform*. PlaceClip adds +0x18 u32 = OR of event flags, +0x1C count, +0x20 events* (12 bytes each:
  SWF ClipEventFlags, code*, third u32 = 0), +0x24 instance name*. Remove: depth u16 at +2. DoAction: code* at +0xC.
- Packed matrix: [sx sy 8.8][rs0 rs1 8.8 if rotate][tx ty int16 twips]; order sx,sy,rs0(b),rs1(c).
- cxform: 8 bytes, mult R,G,B,A in Q2.6 (0x40 = 1.0) then add int8 x4.
- AVM1 code = u16 (1) + standard SWF action bytes ending in 0 (so SWF DoAction/ClipAction bytes are the same).
- Shape: +8 fill table*, +0xC stroke table*, +0x10 display list*, +0x14 u16 fill count, +0x16 u16 stroke count.
  Display list = u16 ops: 4 idx (fill), 5 idx (stroke), 2 n + n*(i16 x,y twips) filled polygon (mostly triangles/quads),
  1 n + points polyline, 0 end. Fill style 0x28 bytes: flag (0 solid, 0x10 linear gradient, 0x40/0x41 repeat/clamped
  bitmap), RGBA, 6 floats, bitmap object*, gradient*. Bitmap fill matrix (engine) maps PIXELS (twips/20) -> UV [0,1]; in SWF the bitmap fill space is bitmap pixels (1:1 display = scale 20).
  Gradient struct: 6 floats mapping shape TWIPS -> SWF gradient space (+-16384) (i.e. inverse of SWF's matrix),
  u16 n, ratio*, RGBA*.  Float matrices are (a,b,c,d,tx,ty) with x'=a x+c y+tx; translations in px for buttons/text.
- Bitmap (type 4): +8 info*, +0xC w,h u16. info+0x90 palette* (0x10 header + RGBA, PS2 alpha 0x80 = opaque),
  info+0x94 ps2Texture*; ps2Texture+8 pixels*, +0x2C w,h, +0x30 type (1 = 8bpp swizzled, 5 = 8bpp linear, 6 = 4bpp).
  8bpp CLUTs are CSM1-swizzled. info (GS upload packets) is cloned from the original; resizing is unsupported.
- Text: matrix(6f) at +8, records* at +0x20: u16 type 1 font(id,size) 2 color 3 y 4 x 5 glyphs(n,(idx,advance)*).
- Button: +9 record count, +0xA cond count, +0xC records* (0x28 each: state bits as SWF, depth u16, 6f matrix(px),
  cxform, target object*), +0x10 conds* (8 bytes: u16 SWF cond flags, u16 extra (unknown), code*).
- Font: +8 u16 n, +0x10 chars* (u16), +0xC table of 2n pointers into OTHER game resources (not in the file) - carried as-is.

## Open questions
Font glyph metrics/bitmaps live outside the pck (fonts are exported as empty glyph shapes; text positions are kept).
EditText fields beyond colour/size/bounds/variable name; button cond `extra` word; swfFILE +0x14 (6) and header words;
unk third event word (0 everywhere except one 0xFFFFFF00); how the engine treats polygons with >4 points (we emit triangles).

## Script semantics (found while debugging Ruffle output)
- SWFs are now written as version 6 (was 7): the movies rely on `n = n + 1` with `n` undefined, which is 0+1 in
  Flash <= 6 but NaN in version 7+. Version 6 is also the minimum for the 32-bit clip event flags.
- Clips are placed off-screen by their placement matrix on purpose (legal: x=+700px, script sets `_x = -700`; loading:
  pict1 y=-479px, script adds `_global.PosPictures`...). Reading the scripts, the engine's `_x/_y` behave as offsets from
  the placement matrix; Flash's are absolute. `pck2swf --preview` bakes the placement offset into a clone of the clip
  (children shifted, clone placed at the origin) so a normal player shows the intended layout. Preview SWFs are not
  convertible back (no embedded pck).  Static text becomes read-only device-font text (glyph outlines are not in the pck).
- A Place on an already occupied depth gets a RemoveObject2 first (Flash ignores it otherwise).
- Many values the scripts read (`_global.StartOfTransitionOut`, `scrollstatus`, `ModeID`, ...) are set by the game.

## Fonts (swfFONT, type 5)
- +8 u16 glyph count n, +0xC table of n pairs of u32 (runtime heap addresses of glyph descriptors - NOT in the file),
  +0x10 u16 char codes[n], +0x14 optional u16 advance table[n] (units of 1/1024 em), +0x38 byte map char code -> glyph index
  (codes < 256, 0 = none). No glyph bitmaps or UVs are stored in these pcks, so fonts can only be rendered if the
  glyph sheets come from another resource. pck2swf therefore borrows glyph outlines from an open TTF (`--font`, default a system DejaVu/Liberation/Arial; needs `pip install fonttools`) and uses the pck's own advance table, so text is visible with the original spacing; characters the TTF lacks become a box. Original glyph look is NOT recovered.
- Edit text (type 7): +8 initial text* (C string, e.g. "152"), +0xC variable name*, +0x10 u16 buffer size, +0x12 u16 flags, +0x14 RGBA, +0x18 font object index, +0x1A size (twips), +0x1C w, +0x20 h, +0x24/+0x28 x,y offset (i32 twips), +0x2C align. Initial text is exported; scripts replace it at runtime.
- Preview baking only applies to clips whose scripts use `_x`/`_y`; a clip that only rotates (the star) keeps its origin
  at its placement so it spins in place instead of orbiting.

## Preview SWFs can be converted back
`pck2swf --preview` now embeds the source pck plus the list of clip clones it made (original char, offset, original
matrix). `swf2pck` detects this, drops the clones/device font, maps placements back to the original characters (undoing the
offset) and applies the usual unchanged/changed logic. Unedited preview -> pck is byte-identical to the original.
Fonts, static text and edit text are never converted back (carried over unchanged).

## Bitmap fonts from .fonttex / .tex (optional)
`pck2swf --fonttex <dir with *.fonttex and texture/*.tex> [--fontmap 30=minidub,33=stencil]` traces the glyph atlas pixels
of a .fonttex into vector outlines (alpha >= 96, pixel edges merged into contours) for the SWF fonts, falling back to the
TTF/box source for characters the fonttex lacks. The pck does not say which fonttex a swf font uses (glyph pointers are
build-tool heap addresses), and the swf advance tables look like the original vector fonts' metrics, so the mapping is a
best guess (glyph coverage + advance/width correlation) and is printed; override with --fontmap. Size calibration: em_px =
1024 / median over the text records' glyph entries of (advance*1024/textSize / (glyph width + spacing)); if no text uses the font, the font's own
advance table is used, else the fonttex height. (legal font 9: 25.6 px vs fonttex height 28; loading font 15: 50.7 px vs 29.) `fontpreview.py` rasterises a font from the generated SWF for checking.

## Embedded bitmap font sheets (the real fonts; supersedes the fonttex guessing)
Found by reading `swfEDITTEXT::Draw` in the asm: glyph units are 1/1024 em, drawn at scale textSize_twips/20480; the pen
advance comes from the font's u16 table at +0x14 (default 0x320); if a SHEET exists whose size equals the text size, the
engine draws bitmap glyphs from it (`SHEET::DrawGlyph`), else the vector `GLYPH::Draw` (whose data is not in the pck).
- swfFONT +0x98 i16 sheet count, +0xA0 sheet pointers (sizes differ per sheet, e.g. 260/1000/1920 twips).
- SHEET: +0 atlas pointer array (one bmpInfo* per page), +4 records*, +8 i16 text size (twips), +0xA u16 glyph count,
  +0x10 u32 page count. Records (12 bytes, one per glyph index, in page order): u8 x,y,w,h (rect in the 128x128 page) and
  i16 left, top, advance, bottom in 1/1024 em (y up negative). The page of a glyph is implicit: it increments whenever the
  record's y jumps back up.
- Atlas page: 4bpp PSMT4-swizzled 128x128 (see `unswizzle4` in rpck.py), 16-entry RGBA palette whose alpha is the glyph mask.
`pck2swf` now uses these sheets by default (the largest size of each font), traced to vector outlines; `--fonttex/--fontmap`
only apply to fonts without sheets or when explicitly mapped. `textcheck.py` renders real text objects for verification.

## Script offsets (from swfINSTANCE::Draw / PlaceObject2::Update in the earlier asm)
Placement is unpacked to a px float matrix (translation = twips * 0.05, scale/rotate * 1/256). A script's `_x/_y`
(inst+0x54/+0x58, initial 0) are applied as T(_x,_y) in the clip's LOCAL space, then R(_rotation), S(_xscale/100):
final = parent . placement . T(_x,_y) . R . S, so offsets are multiplied by the placement's scale. `--preview` reproduces
this: clips with unit scale keep the clone/shift trick; scaled or rotated scripted clips whose name no other script uses
(loading's pict1, 1.24x) get a wrapper sprite carrying the placement with the script clip inside at identity.
Edit text: the engine ignores the box x/y offsets (+0x24/+0x28), starts at the placement origin with the first baseline at
the font's ascent (+0x9A, line extra +0x9C); the SWF box is therefore (0,0,w,h) with Flash's 2px gutter cancelled.

## Preview -> pck: what is and is not lossless
Unedited preview -> pck is byte-identical (all four samples). Edits made in a preview are mapped back: placement moves/matrices
and scripts on baked or wrapped clips (clones and wrappers are replaced by the original clip, clone-of-clone supported), edits
inside a shifted clip copy (un-shifted and applied to the original sprite), shapes, root timeline, same-size bitmaps.
Not mapped back: fonts, static text, edit text, buttons; new bitmaps; edits to wrapper/clone definitions that add content
outside the single script clip. The scripts themselves are never rewritten by preview mode (AVM1 bytes are identical).

## Custom fonts: pckfont.py (TTF/OTF -> engine bitmap sheets)
`python3 pckfont.py in.pck out.pck --font 30=MyFont.ttf[@scale] [--font 33=...] [--add-size TWIPS,...] [--anchor auto|left|center|right]`
`python3 pckfont.py in.pck --info` lists font indices/sheets.  Per font it regenerates every existing sheet at the same size
(twips/20 px; extra sizes appended, max 12 per font): glyphs rasterised with Pillow/FreeType, shelf-packed into 128x128 4bpp
PSMT4-swizzled pages, 16-level alpha through the original palette. Layout written (all pointers appended, old data untouched):
- SHEET (0x20): A -> array of page info*, B -> 12-byte records (x,y,w,h,left,top,advance,bottom), size i16, n u16,
  C -> n-byte page index per glyph (0 when a single page), page count u32.
- Page block (contiguous, 16-aligned): ps2Texture 0x60 (+4 next = previous page's texture, +8 pixels*), 0x2000 pixels, 0x30 pad,
  bmpInfo 0xA0 (+0x90 palette*, +0x94 texture*), palette 0x50.  All page blocks in the sample files are identical to a template
  except those pointers, so the template is cloned from an existing font sheet.
- Font object: +0x98 sheet count, +0xA0 sheet pointers, +0x14 advance table (1/1024 em) rewritten, +0x9A/+0x9C ascent/extra.
- Static TEXT objects using the font get their per-glyph advances recomputed and line start x re-anchored (center/left/right,
  auto-detected from multi-line objects) because the pck bakes advances from the old font.
`@scale` shrinks/grows glyphs relative to the nominal em (default: auto, matches the old font's cap height). The char set
(chars table) is unchanged; characters the TTF lacks render as its .notdef box.
NOT verified in the game (no emulator here): GS upload block reuse, texture-chain `next` pointers, texture memory budget.

## Porting new UIs: swfimport.py (plain SWF -> brand-new pck)
`python3 swfimport.py ui.swf ui.pck --donor any_game.pck [--fit 640x480] [--no-preloader] [--drop-action TEXT] [--sizes TWIPS,...]`
Builds the pck from nothing (header words + font-sheet page template come from a donor pck): root header, object list (object
index == SWF character id), shapes (fills; curves tessellated to triangles; strokes converted to filled polygons because the engine's
stroke units are unknown), sprites + root timeline (place/move/remove, scale/rotation, colour transform, instance names, DoAction),
static text (DefineText -> TEXT records), edit text (initial text buffer, variable name, colour, size, align; box offset folded into
the placement because the engine draws from the placement origin), fonts (DefineFont2/3 outlines rasterised to engine sheets at every
size used, advance table from the layout or the text records). `--fit` scales/centres the root content into the target stage.
Engine limits found in the asm: the interpreter implements 60 AVM1 opcodes (no SetProperty 0x23, GetURL, SetTarget, With, DefineFunction ...);
no getBytesLoaded/Total (use --no-preloader), no masks, no morphs, sound is not converted. Not yet implemented: bitmaps (needs the GS upload
block), buttons, radial gradients (approximated). Verified on badger.swf: all 19 sprite timelines and 904 root frames identical to the source after
reading the pck back, shape coverage within tolerance, pck round-trips through pck2swf/swf2pck. Not tested in the game.

### Size / heap budget
The game loads a pck into a fixed LoadHeap ("Heap LoadHeap overrun (N bytes requested)" when it does not fit; badger's first build of
1,054,672 bytes failed). Original loadscreen pcks are ~490 KB, menus up to ~1.8 MB, but the budget for a given slot is unknown.
swfimport therefore (1) merges triangulations into convex polygons (all 564 multi-point polygons in the original files are convex, so the
engine's own pipeline does the same), (2) flattens curves by tolerance (`--tolerance`, default 4 twips = 0.2 px), (3) emits stroke joins/segments
as convex polygons. badger.pck: 1.05 MB -> 0.30 MB (0.27 MB at 8 twips, 0.35 MB at 2 twips).

### Loader invariants found by diffing against the originals (pckaudit.py)
- no null entries in the object list (SWF character ids are renumbered densely; fonts first);
- the first texture of every original file has `next == 0x245040` (a game global: the texture-list head, identical in all files); later
  textures chain to the previous one. A new file must not copy the donor's interior `next` pointer;
- edit text with no initial text points at the game global 0x214116 (an empty string); ours always has its own buffer.
`python3 pckaudit.py file.pck` checks all of this (pointers inside the file, command lists terminate, characters exist, fonts, sheets,
texture chain) and reports 0 problems for all six original files and the current badger.pck. `swfimport --omit text,actions` builds variants
for bisecting a crash. Still unverified in the engine: null glyph table in built fonts, edit-text/TEXT objects built without the originals' trailing data.

## Bitmaps, buttons, old fonts in swfimport (thellamasong.swf)
- Bitmaps (swfbitmap.py): DefineBits+JPEGTables, JPEG2/3, lossless/lossless2 are decoded with Pillow, stretched over a whole power-of-two texture
  (the originals' convention - see below), quantised to 256 colours (Floyd-Steinberg; alpha via octree) and emitted as 8bpp swizzled
  swfBITMAPs. Every structure is generated from formulas that reproduce all 186 8bpp bitmaps of the sample pcks (pixels, palette, texture
  header, GS upload block) exactly; the object stores 1/sourceSize (originals keep e.g. 1/425 for a logo padded into a 512 texture) and the
  fill matrix is scaled into the occupied part of the texture. The first texture links to the game's global list (0x245040), later ones to the
  previous texture. `--bitmap-budget` (default 600000 bytes) sets the total texture memory; sizes are chosen per image to maximise scale.
- GS upload block (bmpInfo, 0xA0 bytes): +0x40/+0x78 TEX0 low = lw<<26 | (lh&3)<<30 | 0x13<<20 | tbw<<14; +0x44/+0x7C TEX0 high =
  0x20000000 | (lh>>2) | 4; +0x70 = 4 + w*h/256; +0x88 = w,h; +0x90 palette*, +0x94 texture*. Texture header: +0x2A and +0x48 = (w/16)*(h/16),
  +0x32 = h/2, +0x58 = max(1, ceil(w/64)), +0x28 = 5, +0x30 = 1.
- Buttons (DefineButton2): records (0x28 bytes: state bits, depth, px matrix, Q2.6 colour transform, target pointer) and condition actions
  (u16 SWF condition flags, u16 0, code*). Buttons are emitted after their targets.
- DefineFont (v1) + DefineFontInfo/2 are supported (glyph outlines + code table).
- Replace placements (move + new character, no matrix) inherit the previous matrix/colour transform at that depth, as Flash does.
- `--no-preloader` now handles preloaders inside sprites: the getBytesLoaded loader script becomes "goto <loaded frame>" (keeping its
  trailing play/stop) and the loop-back is dropped; if the loader stopped _root, the root is stopped on frame 0 so a start button gates the show.

### Texture convention (corrected after the first llama build mis-mapped UVs)
In every original bitmap whose source size is not a power of two (30 cases, e.g. 79x44 in a 64x32 texture, 425x125 in 512x128, even when the
source is larger than the texture) the shapes' UVs span exactly 0..1: the WHOLE image is stretched over the WHOLE texture, the object stores
1/sourceSize (not 1/textureSize) and the fill matrix undoes the stretch. swfimport now does exactly that (an earlier build padded the image into
a corner, which the engine's clamp/repeat handling did not expect). Textures are limited to width >= height (all 186 originals are; the
ps2Texture fields +0x32 and +0x58 are ambiguous for portrait sizes: h/2 vs min(w,h)/2, w vs max(w,h)).

### Text matrices (found via swfTEXT::Draw / swfMATRIX::Dot in mc3_GPL.asm)
swfMATRIX::Dot(dest, A, B) is the row-vector product dest = A x B (A applied first, then B). swfTEXT::Draw computes
Dot(drawinfoMatrix, textMatrix): the text object's own matrix is applied AFTER the whole placement chain, in screen space, whereas Flash applies
DefineText's matrix first, in local space, so its translation/scale gets multiplied by the placement. Every TEXT matrix in the original pcks is the
identity, so the pipeline evidently keeps text at the origin and puts the offset in the placement. swfimport does the same: TEXT objects get an identity
matrix and the text matrix is composed into every placement of that text (`P' = P after M_t`, also for later move placements).
Static text whose placements scale it up more than `--text-shape-scale` (default 1.75; includes the --fit scale and parent sprites) is converted to a vector
SHAPE (glyph outlines + advances + colour + text matrix baked in): a bitmap font sheet scaled 3.7x would look blocky, and its size no longer needs a sheet.
Other: `--jpeg-tables original.swf` supplies the shared JPEGTables when an editor dropped them (DefineBits would otherwise decode black).

### Tessellator performance / lower poly (tess.py)
Why it was slow: `ear_clip` re-tested *every* polygon vertex against every candidate ear and restarted from vertex 0 after each cut (O(n^3)); on
Salad_Fingers.swf one fill region is a single 10,580-vertex loop (1e12 steps) and `bridge()` then tested every hole connection against every edge
(975 holes). Fixes: linked-list ear clipping that only tests reflex vertices (bbox-rejected), heap + bbox-rejected `bridge()`, bbox-rejected
hole->outer assignment, and a wedge test so a bridge is spliced at the correct copy of an already-bridged (duplicated) vertex (the old code could
splice at the wrong copy, making a self-crossing polygon and a bogus fan fallback, e.g. 3-5x too much area on some Salad Fingers regions).
Lower poly: `--tolerance` only affects curves; traced art is mostly straight `l` edges, so use `--simplify TWIPS` (Douglas-Peucker on each edge
chain, so neighbouring fills keep the same border, no cracks) and `--min-area PX2` (drop specks). 20 twips = 1 px.
Measured on Salad_Fingers.swf: off 85 s / 5.5 MB; --simplify 20 --min-area 2: 12 s / 1.9 MB; 40/4: 6 s / 1.3 MB; 80/8: 4 s / 0.86 MB (polygon
count ~4x/7x/12x lower, pixel difference vs unsimplified 0.2% / 0.5% / 1%). badger: 20 s -> 5 s; --simplify 20 --min-area 2 -> 167 KB.
Not tested in the game.

## v3 changes (on top of the user's tessellator version)
- rpck.read_pmatrix: placement flag1 == 2 (rotation without scale: [r0 r1][tx ty], seen in the ms_vp_* files) is accepted. All 756 sample pcks now parse; audit clean on 400 checked.
- pck2swf: runs of same-fill polygons (the engine stores fills as triangle/quad fans with shared edges) are merged into outline loops before the SWF is written
  (merge_polys: T-junction repair, shared-edge cancellation, collinear points dropped, coverage verified, holes wound oppositely). Reason: JPEXS exports each
  path as separate XFL edges claiming the fill on the right only; the duplicated interior edges of a fan are inconsistent in Flash's one-edge-per-boundary model
  (a gradient quad made of 4 triangles came out as one triangle in the FLA). swfw.shape_records writes merged loops always with FillStyle1.
  Checked: 274 shapes keep their coverage (worst IoU 0.997); round trips pck -> swf -> pck stay byte-identical (faithful and preview).
- pckfont2ttf.py: exports a font object as a TrueType font (outlines traced from the largest bitmap sheet, 1024 units/em = engine units; advances from the
  font's table, else the sheet records, else (spaces) the static text records; ascent/descent from the font object).

## Animated menu backgrounds: pckanim.py (bg_arcade / bg_career / bg_options)
`python3 pckanim.py bg_arcade.pck list | export out.gif [--frames] | replace my.gif out.pck [--fit cover|contain|stretch] [--length original|timing|frames|N] [--opacity PCT] [--dither]`
The background is a looping "movie" stored as a SPRITE (bg_*: object 449, 224 frames, 15 fps from the root frame rate) whose every frame is one Place on depth 1
(frame 0 also carries the matrix, later frames inherit it) of a SHAPE with a single quad (1280x1280 twips = 64x64 px) whose bitmap fill (flag 0x41) is a 64x64 8bpp
swizzled bitmap with its own 256-colour palette; shapes/bitmaps alternate in the object list (frame k = objects 2k+1 bitmap, 2k+2 shape). The movie's placement scales it ~9.7 x 6.8 (about 620x438 px, aspect 1.40-1.44) and draws it with
a colour transform alpha of 22/64 (34 %) in bg_arcade/career/options (the Q2.6 multiply byte is cxform+3). Sprite header: +8 u16 and +0x10 u32 = frame count, +0xC frame table (u32 aux, u32 first command per frame).
bg_options has a second clip (sprite 450, 220 frames, first Place matrix 256x246) that shows the same shapes as the first 220 frames of sprite 449 and is placed on the 4 page variants; sprite 449 itself is used by the 5th.
Replacement is append-only: frames k < old count patch bitmap pixels + palette in place (rpck.bitmap_encode_into), extra frames append a bitmap (swfbitmap.emit_bitmap, its texture chained to the file's last texture),
a cloned shape (0xA0 bytes: header, display list, 2 fill entries, empty stroke table; only the fill's bitmap pointer changes) and a cloned 0x18-byte Place command, then a new frame table and a new object list
(root +0x10 pointer, +0x2A count; new bitmap/shape pairs are appended as objects). Fewer frames only lower the counts (old data stays in the file). Linked clips keep their length difference (new = N - (old_main - old_linked)).
Opacity writes a fresh 8-byte colour transform for every placement of the clip(s). Texture size stays 64x64: each extra frame costs ~5.7 KB and the pck is loaded into a fixed heap, so there is no larger-texture option (replacing 224 frames by
128x128 ones would also leave the old 64x64 data unused in the file, since nothing can be removed from the middle without relocating every pointer).
Verified (selftest_anim.py, three sample pcks): pckaudit clean, frames read back match the source, same/shorter/longer/timing modes, pck2swf -> swf2pck reproduces the written pck byte-identically. NOT verified in the game.
