FROM apify/actor-python:3.12

COPY . ./

RUN python -m pip install --upgrade pip && \
    python -m pip install --no-cache-dir -r requirements.txt

RUN echo "Build OK"

CMD ["python", "-m", "src.main"]
