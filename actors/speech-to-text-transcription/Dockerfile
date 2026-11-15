FROM apify/actor-python:3.12

# CTranslate2 links against OpenMP, and a slim image does not carry it
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The weights live inside the image: a run must not spend its first minute downloading
# 250 MB, and a buyer must not depend on Hugging Face being reachable at that moment.
ENV HF_HOME=/opt/hf
RUN python -c "from faster_whisper import WhisperModel; [WhisperModel(m, device='cpu', compute_type='int8') for m in ('tiny', 'base', 'small')]"
ENV HF_HUB_OFFLINE=1

COPY . ./

CMD ["python", "-m", "src.main"]
