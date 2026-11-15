FROM apify/actor-python:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The checker is the product: the build fails here if the module cannot be imported or
# its punycode path is broken, instead of selling a run that answers "invalid" to all.
COPY src ./src
RUN python -c "import sys; sys.path.insert(0, '.'); from src import checks; print('checks ok', checks.ascii_domain('example.com')[0], checks.typo_for('gmial.com'))"

CMD ["python", "-m", "src.main"]
