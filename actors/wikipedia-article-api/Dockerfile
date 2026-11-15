FROM apify/actor-python:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
# The source is the product: the build fails here if the reader cannot be imported or if
# the input parser goes blind, instead of selling an empty run.
RUN python -c "import sys; sys.path.insert(0, '.'); from src import wiki; a, why = wiki.parse_request('de:Berlin'); assert a and a.lang == 'de' and a.title == 'Berlin', 'lang:title'; assert wiki.parse_request('https://en.wikipedia.org/wiki/Python_(programming_language)')[0].title == 'Python (programming language)', 'urls'; assert wiki.parse_request('not an article at all')[0] is not None, 'plain titles are allowed'; assert wiki.parse_request('https://example.com/wiki/X')[0] is None, 'foreign hosts are refused'; assert hasattr(wiki, 'read_article') and hasattr(wiki, 'read_pageviews'), 'readers'; assert wiki.count_words('one two three') == 3, 'words'; assert wiki.count_words(chr(20013) + chr(25991)) == 2, 'cjk words'; print('reader ok')"

CMD ["python", "-m", "src.main"]
