FROM apify/actor-python:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The encoders are the product. An image without WebP, AVIF or HEIC support would
# refuse half the formats the schema promises, so the build stops here instead of
# selling a promise the container cannot keep.
RUN python -c "from PIL import features; assert features.check('webp'), 'webp is missing'; assert features.check('avif'), 'avif is missing'; import pillow_heif; pillow_heif.register_heif_opener(); print('encoders ok: webp avif heif')"

COPY src ./src

CMD ["python", "-m", "src.main"]
