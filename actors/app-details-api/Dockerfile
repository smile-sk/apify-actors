FROM apify/actor-python:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The two stores are the product: the build fails here if the modules or the Play client
# cannot be imported, instead of selling a run that answers "not found" to every app.
COPY src ./src
RUN python -c "import sys; sys.path.insert(0, '.'); from src import stores; from google_play_scraper import app; print('stores ok', len(stores.classify('389801252')))"

CMD ["python", "-m", "src.main"]
