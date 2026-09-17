# Notice on the original work

This project reads and reproduces the sound engine of **We Are Vocoders**, a 16 KB
intro for the ZX Spectrum by **Dmitry Milk**, released at DiHalt 2026 Summer.

Original repository: https://github.com/DmitryMilk/we_are_vocoders

## What belongs to whom

| | Author | Included here |
|---|---|---|
| The intro, its engine, wavetables, drum samples and note data | Dmitry Milk | **No** |
| Assembler, interpreter, renderers, calibration, web app | this project (MIT) | Yes |

## How this project avoids redistributing it

At the time of writing, the original repository carries **no licence file**.
Under default copyright law that means all rights are reserved: the author has
not granted permission to redistribute his work.

So nothing of his is copied here, and nothing of his is served from a deployment
of this project:

- **The tools** read his repository from wherever you cloned it. Run
  `scripts/fetch-original.sh` and it is fetched from his GitHub, straight to your
  machine.
- **The web app** fetches the original `.asm` and `.dat` sources from
  `raw.githubusercontent.com` **in the visitor's browser**, assembles them there
  and keeps the result in that browser's local storage. The server hosting this
  project never holds or transmits his data; the only copy a visitor receives
  comes from his own published repository. See `web/builder.js`.
- **`web/data/song.json`**, if you build one, *is* a derivative of his work: it
  contains the 57 wavetables, the two drum samples and the engine's state on all
  8,880 ticks. It is optional, it is listed in `.gitignore`, and a deployment
  works without it. The same goes for any rendered audio.

## What this does and does not settle

Fetching from his repository in the visitor's browser is the same kind of act as
embedding or linking: you are pointing at what the author published rather than
republishing it yourself. It is not a licence, and it is not legal advice. Two
practical consequences worth knowing:

- it uses his repository's bandwidth (about 65 requests on a visitor's first
  visit, then nothing — the result is cached in their browser);
- if he renames, moves or removes the repository, a deployment stops working.
  `SRC` at the top of `web/builder.js` is where the path is configured.

Either way, telling him is cheap and he would probably like to know. An issue on
his repository is the obvious place. If you would rather ship a pre-built
`song.json`, ask him first and record the permission in this file.
