# Notice on the original work

This project reads and reproduces the sound engine of **We Are Vocoders**, a 16 KB
intro for the ZX Spectrum by **Dmitry Milk**, released at DiHalt 2026 Summer,
where it placed second.

Original repository: https://github.com/DmitryMilk/we_are_vocoders

## What belongs to whom

| | Author | Licence |
|---|---|---|
| The intro, its engine, wavetables, drum samples and note data | Dmitry Milk | MIT — `LICENSE-we_are_vocoders.txt` |
| Assembler, interpreter, renderers, calibration, web app | this project | MIT — `LICENSE` |

Both halves are MIT, but they are **two separate copyrights**. Keep both notices.

## The original licence

Dmitry Milk licensed his repository under MIT on 2026-10-13 (`LICENSE.txt`,
commit `06795d9`). The MIT licence requires his copyright notice and permission
notice to travel with any copy of his work or any substantial portion of it,
which is why the full text is kept here as `LICENSE-we_are_vocoders.txt`:

> MIT License
>
> Copyright (c) 2026 Dmitry_Milk

## Where his work appears in this repository

The assembler, the interpreter, the synthesisers and the web app are original
code. His work enters at two points, and both are covered by his MIT licence:

- **`web/data/song.json`** is a derivative of it: 57 wavetables, two drum
  samples and the engine's state on all 8,880 ticks, extracted from his `.asm`
  and `.dat` sources. It is committed, and the web app loads it by default.
- **Rendered audio** — anything `vocoders.py render` produces — is a performance
  of his composition. The renders are not committed (see `.gitignore`), but if
  you publish one, credit him.

The Python tools themselves contain none of it: they read *your* clone of his
repository, which `scripts/fetch-original.sh` will fetch for you.

As an alternative to the committed data, `web/builder.js` can assemble the same
song data from his published sources in the visitor's browser, so a deployment
holds nothing of his at all. It produces byte-identical output and is still
there; `USE_LOCAL_DATA` at the top of `web/app.js` chooses between them. With
his licence in place this is now a choice about hosting rather than about rights:
bundling is faster and does not depend on his repository staying where it is.

## If you fork or redistribute this

Ship both licence files, and keep this notice. Credit is the one thing MIT
actually asks for, and in this case the interesting part of the work — the
engine, the instruments, the music — is his.
