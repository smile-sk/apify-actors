FROM apify/actor-python:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
# The reader is the product: the build fails here if the parser or the feed reader goes
# blind, instead of shipping a run that sells nothing.
RUN python -c "import sys; sys.path.insert(0, '.'); from src import arxiv; assert arxiv.parse_paper('1706.03762')[0].key == '1706.03762', 'new id'; assert arxiv.parse_paper('1706.03762v5')[0].version == 5, 'version'; assert arxiv.parse_paper('hep-th/9901001')[0].key == 'hep-th/9901001', 'old id'; assert arxiv.parse_paper('cond-mat.mes-hall/0211022')[0].key == 'cond-mat/0211022', 'old dotted canonical'; assert arxiv.parse_paper('cond-mat.mes-hall/0211022')[0].key == arxiv.parse_paper('cond-mat/0211022')[0].key, 'both forms one key'; assert arxiv.parse_paper('1706.03762v3')[0].request == '1706.03762v3', 'version in request'; assert arxiv.parse_paper('https://arxiv.org/abs/2103.00020v2')[0].version == 2, 'link'; assert arxiv.parse_paper('https://arxiv.org/pdf/2103.00020.pdf')[0].key == '2103.00020', 'pdf link'; assert arxiv.parse_paper('https://example.com/abs/1706.03762')[0] is None, 'foreign host'; assert arxiv.parse_paper('hello world')[0] is None, 'junk'; assert arxiv.parse_paper('')[0] is None, 'empty'; print('reader ok')"

CMD ["python", "-m", "src.main"]
