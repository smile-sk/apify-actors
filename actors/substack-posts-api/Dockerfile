FROM apify/actor-python:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
# The source is the product: the build fails here if the reader cannot be imported or if
# the host parser goes blind, instead of selling an empty run.
RUN python -c "import sys; sys.path.insert(0, '.'); from src import substack; assert substack.as_host('platformer') == 'platformer.substack.com', 'bare subdomains'; assert substack.as_host('https://www.thefp.com/') == 'www.thefp.com', 'urls'; assert substack.as_host('not a host') is None, 'junk'; assert substack.post_row({'id': 1}, 'p', False)['postId'] == '1', 'rows'; print('reader ok')"

CMD ["python", "-m", "src.main"]
