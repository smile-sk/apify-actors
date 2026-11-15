FROM apify/actor-python:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
# The source is the product: the build fails here if the parser or the CDX reader goes
# blind, instead of selling an empty run.
RUN python -c "import sys; sys.path.insert(0, '.'); from src import archive; t, why = archive.parse_target('example.com'); assert t and t.url == 'https://example.com/', 'parse'; assert archive.parse_target('apify.com/pricing')[0].url == 'https://apify.com/pricing', 'path'; assert archive.parse_target('127.0.0.1/x')[0] is None, 'private'; assert archive.parse_target('ftp://example.com/x')[0] is None, 'scheme'; assert archive.parse_target('notaurl')[0] is None, 'no dot'; assert archive.parse_target('example.com:99999')[0] is None, 'bad port'; assert archive.parse_target('https://site.com:abc/x')[0] is None, 'bad port name'; assert archive.parse_target(':65536')[0] is None, 'port range'; assert archive.snapshot_time('20020120142510') == '2002-01-20T14:25:10Z', 'stamp'; assert archive._rows('[]') == [], 'empty'; assert archive._rows('[[\"timestamp\"],[\"20020120142510\"]]')[0]['timestamp'] == '20020120142510', 'rows'; print('reader ok')"

CMD ["python", "-m", "src.main"]
