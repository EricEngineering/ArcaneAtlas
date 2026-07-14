# Arcane Atlas — Instructions

Arcane Atlas is a digital battle-map / virtual tabletop for **in-person** tabletop
RPGs. You lay out maps and tokens on the GM control window, and a separate
**Player Window** mirrors a chosen region of the map onto a second screen or TV
that your players see.

---

## Maps

**Create a map** — right-click in the **Maps** browser (left tabs) → *New Map…*,
and pick the folder. New maps are saved to disk immediately.

**Open a map** — double-click it in the Maps browser, or right-click → *Open*.

**Organize maps** — right-click in the Maps browser to create folders, rename, or
delete. Drag maps between folders.

### Saving

- **File ▸ Save Map** — saves the currently-open map. Player tokens are **not**
  written into the map (they travel with you — see *Player Tokens*).
- **File ▸ Save Map w/ Player Tokens** — saves a snapshot that *includes* the
  current player tokens at their positions.
- You can also right-click the open map in the Maps browser → *Save*.
- Fog of war and the player-view framing are saved automatically when you leave a
  map, so you're only prompted about unsaved **content** changes (items, grid,
  background).

---

## Importing content

Assets live in a library with three categories: **backgrounds**, **objects**, and
**tokens**. Browse them in the **Assets** and **Tokens** tabs.

**Add art to the library** — drag image/video files onto the canvas. You'll be
asked which category it is (background, object, or token). Supported files:

- Images — `.jpg` `.jpeg` `.png` `.webp` (animated / transparent)
- Video — `.mp4` `.webm` `.mov` `.avi` `.m4v`

**Place an asset on the map** — drag it from the Assets/Tokens tab onto the
canvas, or double-click it. Dropping a file with no map open starts a new map
automatically. You can also **right-click the canvas → Import File…** to pick a
file through a dialog (same category prompt as dropping one).

**Manage assets** — right-click in the Assets/Tokens browser for New Folder,
Duplicate, Rename (press **F2** on a selection), Move, or Delete. Renaming/moving
updates every map that references the asset. Assets can only move within their own
category.

**Text boxes** — right-click empty canvas → **Add Text Box** to drop a text
annotation (the one item that isn't imported art). Double-click it (or right-click
→ *Edit Text…*) to edit the words and styling: font size, bold/italic, alignment,
and text/background/border colours. Drag its right-edge handles to set the wrap
width; it moves, rotates, hides-from-players, and copy/pastes like any other item,
and lives in the **objects** layer.

**Layers** — the **Layers** tab lists everything on the map in three bands:
backgrounds (bottom), objects, tokens (top). Drag to reorder within a band; select
a row to select the item on the canvas.

**Locking** — *Lock Assets* freezes backgrounds/objects so you don't nudge them;
*Lock Tokens* freezes tokens. Dragging in a new asset unlocks its category. By
default a map opens with its assets locked (toggle in **File ▸ Settings**).

---

## Tokens

A **token** is a round VTT-style marker baked from any image.

**Create a token** — drop an image and choose the *Token* category. The tokenizer
opens: pan/zoom the image behind the circular crop, choose a size (1×1 … 4×4
inch), and pick the color of the border ring (every token has one).

**On the map** — tokens always snap to the grid and never overlap each other
(they bump to the nearest free cell). They can't be resized on the map; re-edit
size/border via right-click → *Edit Token…* (or from the Tokens browser).

**Recolor / duplicate** — right-click a token for *Change Token Color* (per-copy,
doesn't change the library art) and other actions; *Duplicate* in the browser
makes an independent copy.

**Copy / paste** — `Ctrl+C` / `Ctrl+V` copies canvas items; paste lands at the
cursor. You can also paste an image straight from the system clipboard to import
it as a new asset.

---

## Player View setup

The **Player Window** shows your players a fixed region of the map on a second
screen. A draggable rectangle in the GM view defines exactly what they see.

1. Enable the Player View (the *Playerview* control on the toolbar) and move the
   window to your player-facing screen.
2. Set the display size via the **Dimensions** dialog so grid squares are true
   physical inches — match the **height** to your panel. *Auto* mode derives the
   other dimension from the screen's aspect ratio.
3. Drag the framing rectangle in the GM view to choose what players see; the
   player window follows.

Moving or revealing anything updates both windows automatically — they share one
scene.

**Hide from players** — right-click any item → *Hide from Players*. It stays
visible (with a striped marker) in the GM view but is skipped in the player view.

---

## Fog of War

Fog hides unexplored areas from the players.

- Enable fog, then use the **Reveal** and **Hide** tools to paint. Reveal clears
  fog; Hide paints it back.
- Adjust the brush size and shape (circle/square) on the toolbar, and the GM's fog
  opacity so you can see through your own fog while players can't.
- Fog is saved with the map automatically.
- **Player tokens stay visible through fog** so you can always see where the party
  is.

---

## Pings

Drop a momentary marker to point everyone's eyes at a spot — an expanding ring
that fades on its own after about a second.

- **Alt + left-click** anywhere on the map drops a ping instantly. This works
  even while a Reveal/Hide tool is active and won't disturb your selection.
- Or **right-click the canvas → Ping Here** (available even when assets are
  locked).

A ping shows in **both** the GM and Player windows — and on players' browsers if
Web Sharing is on — and always draws **on top of the fog**.

---

## Player Tokens & the Party

The **Party** is your saved set of player-character tokens.

- **Add to Party** — right-click a token → *Add to Party*. Party tokens show a
  **gold ring**.
- **Party controls** (next to the toolbar) — *Place* puts the party's tokens on
  the current map; *Remove* takes them off; *Disband* clears the roster and
  removes them from the map. The dropdown lists current members.
- **They travel** — player tokens are **not** saved into individual maps. When you
  open another map, they come with you, keeping their positions (bumping only
  where they'd collide with that map's tokens). Use *Save Map w/ Player Tokens* if
  you want to bake them into a specific map.

---

## Web Sharing (LAN)

Let players view the map — and drag their own tokens — from a phone or laptop
browser on the same Wi-Fi.

1. **Web** toolbar button → opens the Web Sharing dialog. Start sharing; players
   scan the QR code or type the URL.
2. Open the Player Window so there's something to stream.
3. Mark which tokens players may move: right-click a token → *Allow Player
   Control* (shown with a blue dashed ring). Only those are draggable remotely.

On the phone: drag empty space to pan, pinch/scroll to zoom, ⟳ to rotate the map,
double-tap to reset.

**Ports & firewall** — sharing uses two TCP ports (a high, uncommon default of
`47800`/`47801`; if those are busy it automatically picks the next free pair, and
the QR/URL always show the real one). Tick **Use a custom port** in the dialog to
choose your own. Your computer's firewall must allow those ports — Windows/macOS
usually pop an “Allow” prompt the first time you start sharing (click **Allow**).
The **Firewall Help…** button opens your firewall settings and shows exactly what
to allow. No internet or router port-forwarding is needed — it's local network only.

---

## Keyboard & Mouse Shortcuts

**Mouse (on the map canvas)**

| Action | Result |
| --- | --- |
| **Left-click** | Select an item |
| **Left-drag** (empty canvas) | Marquee-select multiple items |
| **Ctrl + click** | Add/remove an item from the selection |
| **Alt + left-click** | Drop a ping |
| **Right-drag** or **Middle-drag** | Pan the view |
| **Right-click** (no drag) | Canvas menu (Ping Here, Add Text Box, Import File…) |
| **Double-click** a text box | Edit its text and styling |
| **Ctrl + mouse wheel** | Zoom in/out |
| **Pinch** (macOS trackpad) | Zoom in/out |

**Keyboard**

| Key | Result |
| --- | --- |
| **Ctrl + C** | Copy the selected items |
| **Ctrl + V** | Paste at the cursor (or paste a clipboard image/file as a new asset) |
| **Delete** | Delete the selected items |
| **Esc** | Cancel the current tool (Reveal / Hide) |
| **F2** | Rename the selected item in the Maps / Assets / Tokens browser |

---

## Settings, Instructions & About

Find these under the **File** toolbar button:

- **Settings** — toggle options like showing the player-view box and locking
  assets when opening maps.
- **Instructions** — this document.
- **About** — version, author, and license.
