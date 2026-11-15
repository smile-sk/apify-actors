FROM apify/actor-python:3.12

USER myuser
WORKDIR /home/myuser

COPY --chown=myuser:myuser requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=myuser:myuser . ./

CMD ["python", "-m", "src.main"]
