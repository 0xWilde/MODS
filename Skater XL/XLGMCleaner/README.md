# XLGM Cleaner

Finds the junk in a Skater XL / XLGearModifier gear library and moves it out of the way.
It never deletes anything, and every move can be undone.

## What it finds

| Problem | Why it matters | What the cleaner does |
|---|---|---|
| Two asset packs that are byte-identical copies | Unity logs `AssetBundle ... already loaded` and skips one | moves the copy |
| Two mod folders in `Mods\` with the same Id | Unity Mod Manager refuses one with an error | moves the older one |
| A mesh pack with no texture anywhere (nothing in `Gear\`, nothing embedded) | shows up in the menu but renders blank | moves it |
| A texture in `Gear\` whose prefix matches no mesh (custom pack, skater, or stock template) | wasted disk and load time, never shows up | moves it |
| Two packs that use the same prefix | XLGM only loads one of them | reports only |

Textures in `Decks`, `Grips`, `Trucks`, `Wheels` and `SkateShop` are never touched. Any filename is valid there.

## How to use

1. Unzip the folder anywhere (Desktop is fine). Keep the `python` folder next to the .bat; it is a portable copy of Python, nothing to install.
2. Close Skater XL.
3. Double-click **Run XLGM Cleaner.bat**.
4. Read the report. Nothing has moved yet.
5. Type `MOVE` and press Enter if you agree, or just press Enter to quit.

Everything moved goes to `Documents\SkaterXL_Retired\<date>\` with a `manifest.csv` that lists every file, where it came from, and why.

## Undo

Open a command prompt in the XLGMCleaner folder and run:

```
"Run XLGM Cleaner.bat" --restore "C:\Users\you\Documents\SkaterXL_Retired\2026-10-27_1530\manifest.csv"
```

Every file goes back to exactly where it was.

## Options

All of these can be passed to the .bat (or to `python\python.exe xlgm_cleaner.py`):

```
                      dry run, then asks
--apply               move without asking
--no-prompt           dry run only, never asks (good for scripts)
--gear "D:\..."       custom Gear folder
--packs "D:\..."      custom Asset Packs folder
--game "E:\Steam\steamapps\common\Skater XL"
--mods "E:\Steam\steamapps\common\Skater XL\Mods"
--retire-dir "E:\SkaterXL_Retired"
```

The game folder is auto-detected from Steam. If it isn't found the cleaner still works, using a built-in list of stock gear templates.

## How it decides

The cleaner reads each asset pack directly (Unity bundle format, no Unity needed) and pulls out the real
prefix and alias that XLGM uses to look up textures. A texture matches a mesh when its filename starts with that
prefix followed by `_`, `.`, `-` or a space, so `nhhat_Blue.png`, `nhhat.normal.png` and `nhhat...MaskPBR.png`
all count. Stock game templates like `MShirt`, `TLPants` or `MHatBeanieCuffed` are also recognised, so textures
for the base game's gear are kept.

If a pack has no textures of its own but its mesh is a cut of a stock template (for example a jiggle version of
`MPants`), it is kept and listed under "check yourself", because it may be inheriting the stock textures.

## Requirements

Windows 10/11, 64-bit. Python is included (official python.org embeddable build, see `python\LICENSE.txt`). If you delete the `python` folder the .bat falls back to a system-installed Python 3.8 or newer.
