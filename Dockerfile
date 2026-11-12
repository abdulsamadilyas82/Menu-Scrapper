FROM apify/actor-python-playwright:3.12

COPY requirements.txt ./
RUN python --version && pip --version \
    && pip install -r requirements.txt \
    && playwright install --with-deps chromium

COPY . ./
RUN python -m compileall -q src/

CMD ["python", "-m", "src"]
