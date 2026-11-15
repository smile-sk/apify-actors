FROM apify/actor-python:3.12

# Tesseract lives in the image, so a buyer never needs a key or an OCR service. The
# language packs are the ones the schema promises, and the build stops here if they
# are missing: an image without them would silently return no text.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus tesseract-ocr-ukr tesseract-ocr-osd \
 && rm -rf /var/lib/apt/lists/* \
 && tesseract --version \
 && tesseract --list-langs | grep -qx eng \
 && tesseract --list-langs | grep -qx rus \
 && tesseract --list-langs | grep -qx ukr

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The picture reader is the product: an image without Pillow would refuse every
# file the schema promises, so the build stops instead of selling that promise.
RUN python -c "from PIL import Image; import PIL; print('pillow ok', PIL.__version__)"

COPY src ./src

CMD ["python", "-m", "src.main"]
