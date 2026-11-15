FROM apify/actor-python:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
# The source is the product: the build fails here if the reader cannot be imported or the
# coordinate parser goes blind, instead of selling an empty run.
RUN python -c "import sys; sys.path.insert(0, '.'); from src import weather; assert weather.coordinates('50.45,30.52') == (50.45, 30.52), 'reader'; assert weather.coordinates('Kyiv') is None, 'names are not places'; assert weather.coordinates('+50.45, 30.52') == (50.45, 30.52), 'explicit plus'; print('reader ok')"

CMD ["python", "-m", "src.main"]
