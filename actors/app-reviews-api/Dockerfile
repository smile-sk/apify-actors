FROM apify/actor-python:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The two feeds are the product: the build fails here if the modules cannot be imported,
# instead of selling a run that answers "no reviews" to every app.
COPY src ./src
RUN python -c "import sys; sys.path.insert(0, '.'); from src import apps; print('statuses', len([s for s in dir(apps) if s.startswith('ST_')]))"

CMD ["python", "-m", "src.main"]
