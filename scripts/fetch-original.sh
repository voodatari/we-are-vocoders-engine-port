#!/bin/sh
# Clones (or updates) Dmitry Milk's repository next to this one, which is where
# the Python tools read the song data from. His work is MIT-licensed; see
# NOTICE.md for what came from where.
set -e
DEST="${1:-../we_are_vocoders}"
URL="https://github.com/DmitryMilk/we_are_vocoders"

if [ -d "$DEST/.git" ]; then
  echo "Updating $DEST"
  git -C "$DEST" pull --ff-only
else
  echo "Cloning $URL into $DEST"
  git clone "$URL" "$DEST"
fi
echo
echo "Done. Now:  cd tools && python3 vocoders.py info --repo $DEST"
