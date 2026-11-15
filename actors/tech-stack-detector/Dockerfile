FROM apify/actor-python:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The detector is the product: the build fails here if the fingerprint table cannot be
# imported, instead of selling a run that answers "no technology" to every site.
RUN python -c "import re; print('regex ok')"

COPY src ./src
RUN python -c "import sys; sys.path.insert(0, 'src'); from tech import SIGNATURES; print('signatures', len(SIGNATURES))"

CMD ["python", "-m", "src.main"]
