FROM apify/actor-python-playwright:3.12

COPY requirements.txt ./
# Install the browser AFTER pip: if pip pulls a playwright newer than the one in the
# base image, its bundled chromium is not found and the run dies with
# "executable doesn't exist".
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install chromium

COPY src ./src

CMD ["python", "-m", "src.main"]
