FROM apify/actor-python:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The registry client and the IDN path are the product: the build fails here if the
# module cannot be imported or punycode is broken, instead of selling a run that
# answers "unverified" to every domain.
COPY src ./src
RUN python -c "import sys; sys.path.insert(0, '.'); from src import rdap; print('rdap ok', rdap.parse_domain('münchen.de')[0], rdap.parse_domain('example.com')[0])"

CMD ["python", "-m", "src.main"]
