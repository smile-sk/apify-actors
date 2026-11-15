FROM apify/actor-python:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
# The endpoint is the product: the build fails here if the reader cannot be imported or
# the id parser stops recognising a watch link, instead of selling an empty run.
RUN python -c "import sys; sys.path.insert(0, '.'); from src import yt; assert yt.video_id('https://www.youtube.com/watch?v=jNQXAC9IVRw')[0] == 'jNQXAC9IVRw'; print('reader ok')"

CMD ["python", "-m", "src.main"]
