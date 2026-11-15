FROM apify/actor-python:3.12

# The mp3 encoder and curl live in the image. The build stops here if lame is absent,
# because format='mp3' would then be a promise the actor cannot keep.
RUN apt-get update \
 && apt-get install -y --no-install-recommends lame ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && lame --version

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Voices are baked into the image: a buyer never needs a key or a network. Every file
# is checked for size after the download, so a broken build fails here instead of
# answering "no audio" on a live run.
RUN set -eu; mkdir -p /opt/voices; cd /opt/voices; \
    base=https://huggingface.co/rhasspy/piper-voices/resolve/main; \
    fetch() { curl -fsSL --retry 3 --retry-delay 2 -o "$2.onnx" "$base/$1/$2.onnx"; \
              curl -fsSL --retry 3 --retry-delay 2 -o "$2.onnx.json" "$base/$1/$2.onnx.json"; }; \
    fetch en/en_US/lessac/medium en_US-lessac-medium; \
    fetch en/en_US/amy/medium en_US-amy-medium; \
    fetch ru/ru_RU/irina/medium ru_RU-irina-medium; \
    fetch uk/uk_UA/ukrainian_tts/medium uk_UA-ukrainian_tts-medium; \
    for stem in en_US-lessac-medium en_US-amy-medium ru_RU-irina-medium uk_UA-ukrainian_tts-medium; do \
        test -s "$stem.onnx"; test -s "$stem.onnx.json"; \
    done; \
    python -c "import piper, onnxruntime; print('piper', piper.__name__, 'onnxruntime ok')"

COPY src ./src

CMD ["python", "-m", "src.main"]
