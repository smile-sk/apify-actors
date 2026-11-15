FROM apify/actor-python:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
# The reader is the product: the build fails here if the parser or the feed reader goes
# blind, instead of shipping a run that sells nothing.
RUN python -c "import sys; sys.path.insert(0, '.'); from src import pypi; assert pypi.parse_target('requests')[0].key == 'requests', 'name'; assert pypi.parse_target('requests==2.31.0')[0].version == '2.31.0', 'version'; assert pypi.parse_target('Requests')[0].key == 'requests', 'case folds'; assert pypi.parse_target('zope.interface')[0].key == 'zope-interface', 'dot folds'; assert pypi.parse_target('https://pypi.org/project/requests/2.31.0/')[0].version == '2.31.0', 'link'; assert pypi.parse_target('https://example.com/project/requests/')[0] is None, 'foreign host'; assert pypi.parse_target('hello world')[0] is None, 'junk'; assert pypi.parse_target('')[0] is None, 'empty'; assert pypi.USER_AGENT.startswith('apify-pypi-package-api'), 'user agent'; assert pypi._row({'info': {'name': 'requests', 'version': '2.34.2'}, 'releases': {'2.31.0': []}}, pypi.parse_target('requests==9.9.9')[0], False)[0] is None, 'no such release'; print('reader ok')"

CMD ["python", "-m", "src.main"]
