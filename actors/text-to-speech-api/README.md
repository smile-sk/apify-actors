# Text to Speech API - neural voices, no API key

Most text to speech actors on the store are wrappers over a paid cloud service: you
paste your own API key, you pay per character whether or not the file ever arrives,
and a rate limit on the other side decides how big your batch can be.

This actor speaks inside the run. A small neural voice set is baked into the image,
so there is no key to bring, no quota to hit and no third party that sees the text.
You pay only for a clip that really exists in the run store and can be downloaded.

## What you get

* One row per line of `texts`, with the text, the voice, the length in seconds and the
  key of the audio file.
* The audio itself in the default key-value store, plus one `clips.zip` with every
  clip of the run.
* Four voices in the image: `en-us-lessac`, `en-us-amy`, `ru-ru-irina`,
  `uk-ua-ukrainian`.
* `wav` or `mp3`, and a speed between 0.5 and 2.0.

## What you pay for

One event `clip-synthesized` per delivered clip. Everything else is free:

| Status | Meaning |
| --- | --- |
| `OK` | The clip was made and saved. This is the only billable status. |
| `NOT_CHECKED_BLANK` | The line is empty. |
| `NOT_CHECKED_DUPLICATE` | The same text with the same voice came earlier in the list. |
| `NOT_CHECKED_TOO_LONG` | The line is longer than `maxCharactersPerLine`. Nothing was synthesised. |
| `NOT_CHECKED_LIMIT` | The line is past `maxClips`. |
| `NOT_CHECKED_TIME` | The run was close to the platform deadline. |
| `NOT_CHECKED_CAP` | The spending cap was reached. |
| `NOT_CHECKED_BILLING` | The price could not be read, so nothing was made. |
| `NOT_CHECKED_STORE_ERROR` | The audio could not be saved, so the clip is not delivered. |
| `NOT_CHECKED_SYNTH_ERROR` | The engine could not speak this text. |
| `NOT_CHECKED_ERROR` | The run stopped on an unexpected error before this line. |

## What this actor does not do

* No voice cloning and no custom voice upload: the image carries four fixed voices.
* No language switching inside one clip: one voice speaks the whole line, so pass
  Russian text to a Russian voice.
* No sound effects, no music, no timestamps per word.
* The text is not translated. If the text does not match the voice, the audio is
  still produced and still billed, exactly like any other clip.

## Limits of the size

`maxCharactersPerLine` defaults to 5000 characters, and a longer line is refused
before anything is synthesised. A medium voice makes roughly ten seconds of audio
from 25 words, and the engine runs several times faster than real time on one core,
so a batch of ten short clips finishes in well under a minute.
