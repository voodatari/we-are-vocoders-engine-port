# Notice on the original work

This project reads and reproduces the sound engine of **We Are Vocoders**, a 16 KB
intro for the ZX Spectrum by **Dmitry Milk**, released at DiHalt 2026 Summer.

Original repository: https://github.com/DmitryMilk/we_are_vocoders

## What belongs to whom

| | Author | Included here |
|---|---|---|
| The intro, its engine, wavetables, drum samples and note data | Dmitry Milk | **No** |
| Assembler, interpreter, renderers, calibration, web app | this project (MIT) | Yes |

## Why the song data is not committed

At the time of writing, the original repository carries **no licence file**.
Under default copyright law that means all rights are reserved: the author has
not granted permission to redistribute his work.

`web/data/song.json` is generated from that repository and contains, in
re-encoded form:

- the 57 wavetables (256 bytes each),
- the two drum samples (1,280 bytes each),
- the engine's state on all 8,880 ticks, which is the song itself.

It is therefore a derivative of his work, and it is listed in `.gitignore` so it
never lands in a commit by accident. The same applies to any rendered audio.

## If you want to publish a deployed copy

Ask Dmitry Milk first — an issue on his repository is the obvious place. If he
agrees, record the permission in this file and remove the `.gitignore` entry.

Running the tools locally on your own copy of his repository, for study or for
listening, is a different matter and is what this project is for.
