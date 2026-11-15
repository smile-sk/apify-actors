FROM apify/actor-python-playwright:3.12

# Xvfb is what makes the browser a real window: measured from our VPS, the same
# Chromium passes LinkedIn's wall headful and is refused headless.
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
# Install the browser AFTER pip: if pip pulls a playwright newer than the one in the
# base image, its bundled chromium is not found and every run dies with
# "executable doesn't exist".
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install chromium

COPY src ./src

CMD ["python", "-m", "src.main"]
